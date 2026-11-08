"""MCP2 event methods and webhook safety regression tests; no external sockets."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from haru_life_events.event_bridge_v2 import (
    ALLOWED_EVENT_FIELDS, CallbackError, EventASGI, EventRpc,
    EventSubscriptions, PinnedWebhookClient, event_catalog, validate_callback,
)

NOW = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)
KEY = base64.b64encode(b"k" * 32).decode()
URL = "https://events.example.org/chatgpt/hook"
EVENT = "life.location.changed"
PARAMS = {
    "name": EVENT, "arguments": {},
    "delivery": {"mode": "webhook", "url": URL, "secret": "whsec_" + KEY},
    "cursor": None,
}

class FakeVerifier:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def verify(self, url, secret, sid, now):
        self.calls.append((url, secret, sid, now))
        if self.fail:
            raise CallbackError("challenge_failed")


class SubscriptionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "subs.sqlite"
        self.v = FakeVerifier()
        self.crypto_key = Fernet.generate_key()
        self.store = EventSubscriptions(
            self.path, encryption_key=self.crypto_key,
            identity_key=b"s" * 32, verifier=self.v
        )

    def test_protocol_catalog(self):
        cat = event_catalog()
        self.assertEqual({x["name"] for x in cat}, set(ALLOWED_EVENT_FIELDS))
        for entry in cat:
            self.assertEqual(entry["delivery"], ["webhook"])
            self.assertEqual(entry["inputSchema"]["additionalProperties"], False)
            self.assertEqual(entry["payloadSchema"]["additionalProperties"], False)
            self.assertTrue(entry["payloadSchema"]["required"])

    def test_idempotent_refresh_and_replacement_secret(self):
        first = self.store.subscribe("cat", PARAMS, NOW)
        self.assertTrue(first["id"].startswith("sub_"))
        new_params = {**PARAMS, "delivery": {**PARAMS["delivery"],
                          "secret": "whsec_" + base64.b64encode(b"z"*32).decode()}}
        second = self.store.subscribe("cat", new_params, NOW+timedelta(days=1))
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["refreshBefore"], second["refreshBefore"])
        active = self.store.eligible("cat",EVENT,NOW)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].secret,b"z"*32)
        self.assertEqual(len(self.v.calls),2)

    def test_owner_identity_binding(self):
        owner = self.store.subscribe("cat",PARAMS,NOW)
        other = self.store.subscribe("not-cat",PARAMS,NOW)
        self.assertNotEqual(owner["id"],other["id"])
        self.store.unsubscribe("other", {"name":EVENT,"arguments":{},"delivery":{
            "mode":"webhook","url":URL}})
        self.assertEqual(len(self.store.eligible("cat",EVENT,NOW)),1)
        self.assertEqual(len(self.store.eligible("not-cat",EVENT,NOW)),1)

    def test_unsubscribe_idempotent(self):
        self.store.subscribe("cat",PARAMS,NOW)
        args={"name":EVENT,"arguments":{},"delivery":{"mode":"webhook","url":URL}}
        self.assertEqual(self.store.unsubscribe("cat",args),{})
        self.assertEqual(self.store.unsubscribe("cat",args),{})
        self.assertEqual(self.store.eligible("cat",EVENT,NOW),[])

    def test_challenge_failure_blocks_registration(self):
        self.v.fail=True
        with self.assertRaises(CallbackError):
            self.store.subscribe("cat",PARAMS,NOW)
        self.assertEqual(self.store.eligible("cat",EVENT,NOW),[])

    def test_expiry_and_restart_encrypted_secret(self):
        self.store.subscribe("cat",{**PARAMS,"ttlMs":60000},NOW)
        later=NOW+timedelta(minutes=3)
        restored=EventSubscriptions(self.path, encryption_key=self.crypto_key,
                                   identity_key=b"s"*32,verifier=FakeVerifier())
        self.assertEqual(restored.eligible("cat",EVENT,later),[])
        self.assertNotIn(KEY,self.path.read_bytes().decode(errors="ignore"))
        self.assertNotIn("whsec_",self.path.read_bytes().decode(errors="ignore"))

    def test_wrong_key_never_returns_secret(self):
        self.store.subscribe("cat",PARAMS,NOW)
        restored=EventSubscriptions(self.path,encryption_key=Fernet.generate_key(),
                                   identity_key=b"s"*32,verifier=FakeVerifier())
        with self.assertRaises(CallbackError):
            restored.eligible("cat",EVENT,NOW)

    def test_reject_malformed_subscription(self):
        bads=[
            {**PARAMS,"delivery":{**PARAMS["delivery"],"secret":"abc"}},
            {**PARAMS,"delivery":{**PARAMS["delivery"],"secret":"whsec_"+base64.b64encode(b"a"*5).decode()}},
            {**PARAMS,"delivery":{**PARAMS["delivery"],"url":"http://events.example.org/a"}},
            {**PARAMS,"name":"life.exact_coordinates"},
            {**PARAMS,"arguments":{"lat":"35"}},
            {**PARAMS,"cursor":"replay"},
            {**PARAMS,"ttlMs":5},
        ]
        for bad in bads:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                self.store.subscribe("cat",bad,NOW)

    def test_missing_verifier_failclosed(self):
        insecure=EventSubscriptions(self.path,encryption_key=self.crypto_key,
                                    identity_key=b"s"*32)
        with self.assertRaises(CallbackError):
            insecure.subscribe("cat",PARAMS,NOW)


class TransportTest(unittest.TestCase):
    def test_private_addresses_rejected_by_syntax(self):
        for url in [
            "http://example.org/path","https://127.0.0.1/cb",
            "https://localhost/cb","https://service.internal/cb",
            "https://admin:pw@example.org/cb","https://example.org:8443/cb",
            "https://example.org/cb?secret=a","https://example.org/cb#hash"
        ]:
            with self.subTest(url=url),self.assertRaises(ValueError):
                validate_callback(url)

    def test_dns_rebind_blocked(self):
        client=PinnedWebhookClient()
        with patch("socket.getaddrinfo", return_value=[
            (2,1,6,"",("192.168.1.1",443)),
            (2,1,6,"",("8.8.8.8",443)),
        ]):
            with self.assertRaises(CallbackError) as exc:
                client._address("events.example.org")
        self.assertEqual(exc.exception.reason,"unsafe_destination")

    def test_mock_challenge_signature_and_echo(self):
        class Echo(PinnedWebhookClient):
            def post(self,url,body,headers):
                self.payload=json.loads(body)
                self.headers=headers
                self.raw=body
                return 200, json.dumps({"challenge":self.payload["challenge"]}).encode()
        echo=Echo()
        echo.verify(URL,b"k"*32,"sub_123",NOW)
        h=echo.headers
        sig=base64.b64encode(hmac.new(
            b"k"*32, (h["webhook-id"]+"."+h["webhook-timestamp"]+".").encode()+echo.raw,
            hashlib.sha256).digest()).decode()
        self.assertEqual(h["webhook-signature"],"v1,"+sig)
        self.assertEqual(h["X-MCP-Subscription-Id"],"sub_123")

    def test_challenge_mismatch_rejected(self):
        class Wrong(PinnedWebhookClient):
            def post(self,url,body,headers):
                return 200,b'{"challenge":"not-the-nonce"}'
        with self.assertRaises(CallbackError) as exc:
            Wrong().verify(URL,b"k"*32,"sub_123",NOW)
        self.assertEqual(exc.exception.reason,"challenge_failed")

    def test_signed_event_payload_uses_exact_body(self):
        class Fake(PinnedWebhookClient):
            def post(self,url,body,headers):
                self.raw=body;self.headers=headers
                return 202,b""
        client=Fake()
        event={"eventId":"evt_abc12345","name":EVENT,
               "timestamp":"2026-10-11T13:00:00Z",
               "data":{"place":"千代田区, 東京都","observed_at":"2026-10-11T13:00:00Z","reason":"distance_2km"},"cursor":None}
        status=client.deliver(url=URL,secret=b"k"*32,sid="sub_123",event=event,now=NOW)
        self.assertEqual(status,202)
        self.assertEqual(client.headers["webhook-id"],event["eventId"])
        self.assertEqual(json.loads(client.raw),event)


class RPCTransportTest(SubscriptionsTest):
    def test_rpc_discover_and_events_list(self):
        rpc=EventRpc(self.store)
        for method in ("server/discover","events/list","tools/list"):
            payload=rpc.invoke({"jsonrpc":"2.0","id":1,"method":method,"params":{}},
                               owner="cat",now=NOW)
            self.assertIn("result",payload)
        self.assertEqual(rpc.invoke({"jsonrpc":"2.0","id":2,"method":"server/discover"},
                                    owner="cat",now=NOW)["result"]["supportedVersions"],
                         ["2026-07-28"])

    def test_rpc_rejected_callback(self):
        self.v.fail=True
        rpc=EventRpc(self.store)
        response=rpc.invoke({"jsonrpc":"2.0","id":3,"method":"events/subscribe",
                             "params":PARAMS},owner="cat",now=NOW)
        self.assertEqual(response["error"]["code"],-32015)
        self.assertEqual(response["error"]["data"]["reason"],"challenge_failed")

    def test_rpc_no_owner(self):
        rpc=EventRpc(self.store)
        response=rpc.invoke({"jsonrpc":"2.0","id":3,"method":"events/list"},
                            owner="",now=NOW)
        self.assertIn("error",response)

    def test_asgi_wire_unauthorized_and_authenticated(self):
        rpc=EventRpc(self.store)
        app=EventASGI(rpc,authenticate=lambda hdr:
            "cat" if hdr.get("authorization")=="Bearer simulated-test-token" else None)
        async def call(authorization="Bearer simulated-test-token",
                       version="2026-07-28",meta_version="2026-07-28"):
            body={"jsonrpc":"2.0","id":23,"method":"events/list",
                  "params":{"_meta":{
                      "io.modelcontextprotocol/protocolVersion":meta_version,
                      "io.modelcontextprotocol/clientCapabilities":{}}}}
            frames=[{"type":"http.request","body":json.dumps(body).encode(),"more_body":False}]
            output=[]
            async def receive():return frames.pop(0)
            async def send(msg):output.append(msg)
            await app({
                "type":"http","method":"POST","path":"/mcp",
                "headers":[(b"authorization",authorization.encode()),
                           (b"mcp-protocol-version",version.encode()),
                           (b"mcp-method", b"events/list"),
                           (b"content-type", b"application/json")]
            },receive,send)
            return output
        self.assertEqual(asyncio.run(call())[0]["status"],200)
        self.assertEqual(asyncio.run(call("Bearer invalid"))[0]["status"],401)
        self.assertEqual(asyncio.run(call(version="2025-11-25"))[0]["status"],400)
        self.assertEqual(asyncio.run(call(meta_version="2025-11-25"))[0]["status"],400)

if __name__=="__main__":
    unittest.main()
