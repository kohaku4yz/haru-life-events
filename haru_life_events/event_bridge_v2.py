"""MCP 2026-07-28 events RPC and signed webhook core (isolated, not deployed).

No OAuth provider is bundled: the injected authenticate callback MUST verify
credentials and derive the actual principal. Missing authentication or callback
verification fails closed. No live listener is installed by this module.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import sqlite3
import ssl
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from .location_payload import LOCATION_PAYLOAD_SCHEMA, location_data_valid
from .vitals_event_payload import valid_vitals_data
from .context_payload import CONTEXT_NAME, CONTEXT_PAYLOAD_SCHEMA

PROTOCOL_VERSION = "2026-07-28"
MAX_EVENT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8192
# Events a Work task can subscribe to. Vitals reach Work only inside CONTEXT_NAME.
ALLOWED_EVENT_FIELDS = {
    CONTEXT_NAME: {},  # the one shared Work listener: location OR Vitals change
    "life.location.changed": {},  # location-only fallback while no context subscription exists
}


def _utc(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat().replace("+00:00", "Z")


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_name_args(name: object, arguments: object) -> tuple[str, str]:
    if not isinstance(name, str) or name not in ALLOWED_EVENT_FIELDS:
        raise ValueError("unknown event")
    if not isinstance(arguments, dict) or arguments:
        raise ValueError("this event takes no filter arguments")
    return name, "{}"


def _validate_secret(value: object) -> bytes:
    if not isinstance(value, str) or not value.startswith("whsec_"):
        raise ValueError("callback secret must start with whsec_")
    try:
        key = base64.b64decode(value[6:], validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("invalid webhook secret") from None
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid signing key length")
    return key


def validate_callback(url: object) -> str:
    """Only *syntax* checks; actual delivery additionally validates resolved IPs."""
    if not isinstance(url, str) or not 12 <= len(url) <= 2048:
        raise ValueError("invalid callback URL")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise ValueError("malformed callback URL") from None
    if p.scheme != "https" or not p.hostname or port not in (None, 443):
        raise ValueError("callback must use HTTPS with port 443")
    if p.username or p.password or p.fragment or p.query or "\\" in url or not p.path.startswith("/"):
        raise ValueError("callback URL contains disallowed fields")
    hostname = p.hostname
    if hostname.endswith((".local", ".localhost", ".internal")) or hostname == "localhost":
        raise ValueError("private hostname")
    if hostname.endswith(".") or ".." in hostname or not all(
        part and len(part) <= 63 for part in hostname.split(".")
    ):
        raise ValueError("ambiguous hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP literal not accepted")
    return url


def sign(secret: bytes, body: bytes, message_id: str, timestamp: int) -> dict[str, str]:
    signed = f"{message_id}.{timestamp}.".encode() + body
    digest = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
    return {
        "Content-Type": "application/json",
        "webhook-id": message_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": "v1," + digest,
    }


class CallbackError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to previously checked IP with original SNI and Host verification."""

    def __init__(self, hostname: str, address: str, timeout: int = 10):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class PinnedWebhookClient:
    """HTTPS transport; each request resolves AND validates actual dial address.

    No implicit proxies, redirects or public endpoint configuration. Forbidden
    addresses fail before any socket is opened. Never log the full callback URL.
    """

    def _address(self, hostname: str) -> str:
        try:
            answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CallbackError("dns_failed") from exc
        addresses = {entry[4][0].split("%", 1)[0] for entry in answers}
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise CallbackError("unsafe_destination")
        return sorted(addresses)[0]

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        validate_callback(url)
        if len(body) > MAX_EVENT_BYTES:
            raise CallbackError("oversized_event")
        p = urlsplit(url)
        assert p.hostname
        address = self._address(p.hostname)
        conn = _PinnedHTTPSConnection(p.hostname, address)
        path = p.path or "/"
        try:
            conn.request("POST", path, body=body, headers=dict(headers))
            resp = conn.getresponse()
            result = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(result) > MAX_RESPONSE_BYTES:
                raise CallbackError("response_too_large")
            return resp.status, result
        except (OSError, ssl.SSLError, TimeoutError, http.client.HTTPException) as exc:
            raise CallbackError("transport_failed") from exc
        finally:
            conn.close()

    def verify(self, url: str, secret: bytes, sid: str, now: datetime) -> None:
        challenge = secrets.token_urlsafe(24)
        message_id = "msg_verification_" + secrets.token_hex(12)
        body = json.dumps({"type": "verification", "challenge": challenge},
                          separators=(",", ":")).encode()
        headers = sign(secret, body, message_id, int(_utc(now).timestamp()))
        headers["X-MCP-Subscription-Id"] = sid
        status, raw = self.post(url, body, headers)
        if not 200 <= status < 300:
            raise CallbackError("challenge_failed")
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CallbackError("challenge_failed") from None
        response_challenge = decoded.get("challenge") if isinstance(decoded, dict) else None
        if not isinstance(response_challenge, str) or not hmac.compare_digest(challenge, response_challenge):
            raise CallbackError("challenge_failed")

    def deliver(self, *, url: str, secret: bytes, sid: str,
                event: dict[str, Any], now: datetime) -> int:
        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_EVENT_BYTES:
            raise CallbackError("oversized_event")
        headers = sign(secret, body, event["eventId"], int(_utc(now).timestamp()))
        headers["X-MCP-Subscription-Id"] = sid
        status, _ = self.post(url, body, headers)
        return status


class CallbackVerifier(Protocol):
    def verify(self, url: str, secret: bytes, sid: str, now: datetime) -> None: ...


@dataclass(frozen=True)
class ActiveSubscription:
    id: str
    callback: str
    secret: bytes
    name: str


class EventSubscriptions:
    """Verified, owner-isolated subscriptions. Encrypted key is owner-supplied.

    Real bearer/OAuth authentication belongs *outside* this class. A principal
    is never accepted from JSON-RPC params. The caller must be trusted.
    """

    def __init__(self, path: str | Path, *, encryption_key: bytes,
                 identity_key: bytes, verifier: CallbackVerifier | None = None):
        if not isinstance(identity_key, bytes) or len(identity_key) < 32:
            raise ValueError("identity key must be at least 32 bytes")
        self.crypt = Fernet(encryption_key)
        self.identity_key = identity_key
        self.verifier = verifier
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    name TEXT NOT NULL, arguments TEXT NOT NULL,
                    url TEXT NOT NULL, encrypted_secret BLOB NOT NULL,
                    refreshed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
            """)
            conn.commit()

    def _identity(self, owner: str, name: str, canonical_args: str, url: str) -> str:
        if not isinstance(owner, str) or not 1 <= len(owner) <= 256:
            raise ValueError("missing authenticated principal")
        record = _canonical({"owner": owner, "name": name, "arguments": canonical_args, "url": url})
        digest = hmac.new(self.identity_key, record.encode(), hashlib.sha256).hexdigest()[:40]
        return "sub_" + digest

    def subscribe(self, owner: str, params: dict[str, Any], now: datetime) -> dict[str, Any]:
        if self.verifier is None:
            raise CallbackError("callback_verifier_unconfigured")
        if not isinstance(params, dict):
            raise ValueError("invalid subscription parameters")
        name, args = _validate_name_args(params.get("name"), params.get("arguments", {}))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise ValueError("only webhook is supported")
        url = validate_callback(delivery.get("url"))
        secret = _validate_secret(delivery.get("secret"))
        if params.get("cursor") is not None:
            raise ValueError("nonreplayable event requires null cursor")
        requested_ttl = params.get("ttlMs", 7 * 24 * 3600 * 1000)
        if requested_ttl is None:
            ttl = timedelta(days=7)
        elif isinstance(requested_ttl, int) and not isinstance(requested_ttl, bool) and requested_ttl >= 60000:
            ttl = min(timedelta(milliseconds=requested_ttl), timedelta(days=30))
        else:
            raise ValueError("invalid ttlMs")
        sid = self._identity(owner, name, args, url)
        instant = _utc(now)
        # Challenge MUST be sent and echoed before any subscription is activated.
        self.verifier.verify(url, secret, sid, instant)
        expires_at = _iso(instant + ttl)
        encrypted = self.crypt.encrypt(secret)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO subscriptions
                 (id,owner,name,arguments,url,encrypted_secret,refreshed_at,expires_at,active)
                VALUES (?,?,?,?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET
                    encrypted_secret=excluded.encrypted_secret,
                    refreshed_at=excluded.refreshed_at,
                    expires_at=excluded.expires_at, active=1
                WHERE owner=excluded.owner
            """, (sid, owner, name, args, url, encrypted, _iso(instant), expires_at))
            conn.commit()
        return {"id": sid, "refreshBefore": expires_at, "cursor": None, "truncated": False}

    def unsubscribe(self, owner: str, params: dict[str, Any]) -> dict:
        if not isinstance(params, dict):
            raise ValueError("invalid unsubscribe parameters")
        name, args = _validate_name_args(params.get("name"), params.get("arguments", {}))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise ValueError("invalid delivery")
        url = validate_callback(delivery.get("url"))
        sid = self._identity(owner, name, args, url)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE subscriptions SET active=0 WHERE id=? AND owner=?", (sid, owner))
            conn.commit()
        return {}

    def eligible(self, owner: str, name: str, now: datetime) -> list[ActiveSubscription]:
        """Private sender-facing read, never callable through public JSON-RPC."""
        _validate_name_args(name, {})
        if not isinstance(owner, str) or not owner:
            raise ValueError("authenticated owner required")
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("""
                SELECT id,url,encrypted_secret FROM subscriptions
                WHERE owner=? AND name=? AND active=1 AND expires_at>?
                ORDER BY id LIMIT 32
            """, (owner, name, _iso(now))).fetchall()
        result = []
        for sid, url, encrypted in rows:
            try:
                key = self.crypt.decrypt(encrypted)
            except InvalidToken:
                # Wrong/revoked vault key is a hard stop, never send unsigned.
                raise CallbackError("secret_decryption_failed") from None
            result.append(ActiveSubscription(sid, url, key, name))
        return result


def event_catalog() -> list[dict[str, Any]]:
    items = []
    for name in ALLOWED_EVENT_FIELDS:
        schema = CONTEXT_PAYLOAD_SCHEMA if name == CONTEXT_NAME else LOCATION_PAYLOAD_SCHEMA
        detail = ("One owner-authorized context listener: coarse location change OR "
                  "PulsHealth Vitals transition. Type-discriminated; no raw GPS or health history."
                  if name == CONTEXT_NAME else
                  "Owner-authorized coarse location change (location-only fallback); "
                  "no raw GPS or personal free text.")
        items.append({
            "name": name,
            "description": detail,
            "delivery": ["webhook"],
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "payloadSchema": schema,
        })
    return items


class EventRpc:
    """Transport-agnostic JSON-RPC 2.0 handler.

    The outer HTTP layer must validate MCP2 version metadata and obtain owner
    from verified OAuth. Do NOT derive principal from request parameters.
    """

    def __init__(self, store: EventSubscriptions):
        self.store = store

    def invoke(self, request: dict[str, Any], *, owner: str, now: datetime) -> dict[str, Any]:
        rid = request.get("id") if isinstance(request, dict) else None
        result: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if not isinstance(owner, str) or not owner:
            result["error"] = {"code": -32001, "message": "Authentication required"}
            return result
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            result["error"] = {"code": -32600, "message": "Invalid Request"}
            return result
        try:
            method = request.get("method")
            if method == "server/discover":
                response = {
                    "resultType": "complete",
                    "supportedVersions": [PROTOCOL_VERSION],
                    "capabilities": {"tools": {}, "events": {}},
                }
            elif method == "tools/list":
                response = {"tools": []}
            elif method == "events/list":
                params = request.get("params", {})
                if not isinstance(params, dict) or params.get("cursor") not in (None, ""):
                    raise ValueError("invalid cursor")
                response = {"events": event_catalog()}
            elif method == "events/subscribe":
                response = self.store.subscribe(owner, request.get("params", {}), now)
            elif method == "events/unsubscribe":
                response = self.store.unsubscribe(owner, request.get("params", {}))
            else:
                result["error"] = {"code": -32601, "message": "Method not found"}
                return result
            result["result"] = response
        except CallbackError as exc:
            result["error"] = {
                "code": -32015, "message": "CallbackEndpointError",
                "data": {"reason": exc.reason},
            }
        except ValueError:
            result["error"] = {"code": -32602, "message": "Invalid params"}
        return result


class EventASGI:
    """Testable ASGI /mcp wire adapter; deliberately no listener or auth server.

    Production must inject OAuth token validation bound to the correct owner.
    The injected authenticator must return None for any invalid/missing token.
    """

    def __init__(self, rpc: EventRpc,
                 authenticate: Callable[[Mapping[str, str]], str | None] | None = None,
                 audit: Callable[[str, str], None] | None = None):
        self.rpc = rpc
        self.authenticate = authenticate
        self.audit = audit

    def record_diagnostic(self, method: str, reason: str) -> None:
        """Only fixed, non-sensitive method/reason codes reach audit storage."""
        if self.audit is None:
            return
        allowed_methods = {
            "server/discover", "events/list", "events/subscribe",
            "events/unsubscribe", "initialize", "notifications/initialized",
        }
        allowed_reasons = {
            "bad_content_type", "bad_json", "bad_request_method",
            "bad_legacy_initialize", "bad_legacy_notification",
            "bad_protocol_header", "bad_method_header", "unexpected_name_header",
            "bad_params_object", "bad_protocol_meta", "rpc_invalid_params",
            "rpc_callback_error", "rpc_success", "bad_duplicate_headers",
        }
        name = method if method in allowed_methods else "other"
        code = reason if reason in allowed_reasons else "other"
        try:
            self.audit(name, code)
        except Exception:
            # Observability can never block the MCP request.
            pass

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise RuntimeError("only HTTP supported")
        raw_headers = [(k.lower().decode(), v.decode()) for k, v in scope.get("headers", [])]
        # Duplicate identity/routing headers risk disagreement between proxies.
        guarded_headers = {"authorization", "mcp-method", "mcp-protocol-version", "mcp-name",
                           "content-type"}
        if len([key for key, _ in raw_headers if key in guarded_headers]) != len(
            {key for key, _ in raw_headers if key in guarded_headers}
        ):
            self.record_diagnostic("other", "bad_duplicate_headers")
            raw_headers = []
        headers = dict(raw_headers)
        async def reply(status: int, data: dict[str, Any]):
            raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(raw)).encode())]})
            await send({"type": "http.response.body", "body": raw})
        if scope.get("method") != "POST" or scope.get("path") != "/mcp":
            await reply(404, {"error": "not_found"})
            return
        if not self.authenticate:
            await reply(401, {"error": "unauthorized"})
            return
        owner = self.authenticate(headers)
        if not owner:
            await reply(401, {"error": "unauthorized"})
            return
        if headers.get("content-type", "").split(";", 1)[0].lower().strip() != "application/json":
            self.record_diagnostic("other", "bad_content_type")
            await reply(415, {"error": "json_content_type_required"})
            return
        data = bytearray()
        while True:
            item = await receive()
            if item["type"] == "http.disconnect":
                return
            data.extend(item.get("body", b""))
            if len(data) > 65536:
                await reply(413, {"error": "request_too_large"})
                return
            if not item.get("more_body", False):
                break
        method = "other"
        reason = "bad_json"
        try:
            request = json.loads(bytes(data))
            if not isinstance(request, dict):
                raise ValueError
            method = request.get("method")
            reason = "bad_request_method"
            if not isinstance(method, str) or not method:
                raise ValueError
            # Legacy tunnel-client 0.0.11 performs a standard MCP initialize
            # probe before forwarding MCP2 event requests. This narrow,
            # authenticated compatibility response DOES NOT enable any
            # legacy tools, subscriptions or unversioned RPC methods.
            if method == "initialize":
                params = request.get("params")
                client_version = params.get("protocolVersion") if isinstance(params,dict) else None
                supported_legacy = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
                if (
                    request.get("jsonrpc") == "2.0" and
                    isinstance(request.get("id"), (int, str)) and
                    not isinstance(request.get("id"), bool) and
                    client_version in supported_legacy and
                    headers.get("mcp-method") in (None, "initialize") and
                    headers.get("mcp-protocol-version") in (None, client_version)
                ):
                    await reply(200, {
                        "jsonrpc": "2.0", "id": request["id"],
                        "result": {
                            "protocolVersion": client_version,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "haru-life-events", "version": "0.1.0"},
                        },
                    })
                    return
                reason = "bad_legacy_initialize"
                raise ValueError
            if method == "notifications/initialized":
                if (
                    request.get("jsonrpc") == "2.0" and "id" not in request and
                    headers.get("mcp-method") in (None, "notifications/initialized") and
                    headers.get("mcp-protocol-version") != PROTOCOL_VERSION
                ):
                    await reply(202, {})
                    return
                reason = "bad_legacy_notification"
                raise ValueError
            reason = "bad_protocol_header"
            if headers.get("mcp-protocol-version") != PROTOCOL_VERSION:
                raise ValueError
            # Modern MCP requires routable method headers; reject ambiguity.
            reason = "bad_method_header"
            if headers.get("mcp-method") != method:
                raise ValueError
            reason = "bad_params_object"
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError
            # MCP 2.0 events requests may route by the event name, just as
            # tool calls route by tool name. Never trust a name header alone:
            # if it exists, it must identify the exact event in the body.
            reason = "unexpected_name_header"
            if "mcp-name" in headers:
                if method in ("events/subscribe", "events/unsubscribe"):
                    if (
                        not isinstance(params.get("name"), str) or
                        headers["mcp-name"] != params["name"]
                    ):
                        raise ValueError
                elif method not in ("tools/call", "prompts/get", "resources/read"):
                    raise ValueError
            reason = "bad_protocol_meta"
            meta = params.get("_meta", {})
            if (not isinstance(meta, dict) or
                    meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL_VERSION or
                    not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict)):
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            self.record_diagnostic(method, reason)
            await reply(400, {"error": "invalid_mcp2_envelope"})
            return
        # MCP-specific metadata is not subscription filter data.
        p = dict(request.get("params", {}))
        p.pop("_meta", None)
        req = dict(request, params=p)
        response = self.rpc.invoke(req, owner=owner, now=datetime.now(timezone.utc))
        rpc_error = response.get("error")
        if isinstance(rpc_error, dict):
            code = rpc_error.get("code")
            if code == -32602:
                self.record_diagnostic(method, "rpc_invalid_params")
            elif code == -32015:
                self.record_diagnostic(method, "rpc_callback_error")
        else:
            self.record_diagnostic(method, "rpc_success")
        await reply(200, response)


__all__ = [
    "EventASGI", "EventRpc", "EventSubscriptions", "PinnedWebhookClient",
    "CallbackError", "ALLOWED_EVENT_FIELDS", "event_catalog",
]
