"""Explicitly gated localhost entrypoint for single-owner MCP Events.

Not enabled by import. Does not modify the existing MCP 1.x service or its
tunnel. Secrets must live in private files and must never be logged.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping

from .event_bridge_v2 import EventASGI, EventRpc, EventSubscriptions, PinnedWebhookClient
from .tunnel_owner_auth import SingleOwnerTunnelAuth


def _secret_file(path: str | None, *, minimum: int = 32) -> bytes:
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("absolute secret file path required")
    file = Path(path)
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("secret file must be regular with no group/other access")
    if info.st_size > 4096:
        raise ValueError("secret file too large")
    secret = file.read_bytes().rstrip(b"\n")
    if len(secret) < minimum:
        raise ValueError("secret must have at least 32 bytes")
    return secret


def build_single_owner_app(*, settings: Mapping[str, str]):
    """Construct an app only with explicit enablement and valid private config.

    Separate credentials are required:
    * HARU_EVENTS_API_KEY_FILE: bearer sent by the ChatGPT plugin connection
    * HARU_EVENTS_FERNET_KEY_FILE: at-rest signing key encryption
    * HARU_EVENTS_ID_KEY_FILE: deterministic subscription IDs
    """
    if settings.get("HARU_EVENTS_BRIDGE_ENABLED") != "1":
        raise RuntimeError("MCP Events bridge is not enabled")
    owner = settings.get("HARU_EVENTS_OWNER")
    if not isinstance(owner, str) or not 1 <= len(owner) <= 128:
        raise ValueError("configured single owner missing")
    db = settings.get("HARU_EVENTS_SUBSCRIPTIONS_DB")
    if not isinstance(db, str) or not Path(db).is_absolute():
        raise ValueError("absolute subscription database path required")
    if not Path(db).parent.is_dir():
        raise ValueError("subscription database parent must already exist")
    bearer = _secret_file(settings.get("HARU_EVENTS_API_KEY_FILE"))
    vault = _secret_file(settings.get("HARU_EVENTS_FERNET_KEY_FILE"))
    identity = _secret_file(settings.get("HARU_EVENTS_ID_KEY_FILE"))
    auth = SingleOwnerTunnelAuth(owner=owner, key=bearer)
    store = EventSubscriptions(db, encryption_key=vault, identity_key=identity,
                               verifier=PinnedWebhookClient())
    audit = None
    audit_path = settings.get("HARU_EVENTS_SAFE_AUDIT_FILE")
    if audit_path:
        from .wire_audit import SafeWireAudit
        audit = SafeWireAudit(Path(audit_path))
    return EventASGI(EventRpc(store), authenticate=auth, audit=audit)


def main() -> None:
    """Run only on localhost; opt-in operator action required."""
    import uvicorn
    app = build_single_owner_app(settings=os.environ)
    port = int(os.environ.get("HARU_EVENTS_LOOPBACK_PORT", "8794"))
    if not 1024 <= port <= 65535:
        raise ValueError("invalid loopback port")
    # Never accept a caller-supplied host override.
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
