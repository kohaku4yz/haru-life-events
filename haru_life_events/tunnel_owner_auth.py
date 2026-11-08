"""Strict one-owner MCP bearer adapter for a PRIVATE tunnel, not OAuth.

This does not assume that tunnelling authenticates application principals.
The caller must configure a separate 32+ byte random service API key in a
private environment and arrange for a connected plugin to send it. The
single fixed owner name comes only from local server configuration.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class SingleOwnerTunnelAuth:
    owner: str
    key: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not 1 <= len(self.owner) <= 128:
            raise ValueError("configured owner identity is required")
        if not isinstance(self.key, bytes) or len(self.key) < 32:
            raise ValueError("configured bearer key must be at least 32 bytes")
        if any(ch in self.owner for ch in "\r\n\t"):
            raise ValueError("bad configured owner")

    def __call__(self, headers: Mapping[str, str]) -> str | None:
        """Derive owner only from an exact fixed bearer, never a client claim."""
        if not isinstance(headers, Mapping):
            return None
        auth = headers.get("authorization")
        if not isinstance(auth, str) or len(auth) > 4096 or not auth.startswith("Bearer "):
            return None
        candidate = auth[7:]
        # No trimming, no whitespace, no prefix matching, no Basic credentials.
        try:
            supplied = candidate.encode("ascii")
        except UnicodeEncodeError:
            return None
        if len(supplied) < 32 or any(x <= 32 or x >= 127 for x in supplied):
            return None
        # Hash both sides so comparisons have a fixed output length.
        desired = hashlib.sha256(self.key).digest()
        received = hashlib.sha256(supplied).digest()
        return self.owner if hmac.compare_digest(desired, received) else None


__all__ = ["SingleOwnerTunnelAuth"]
