"""Opt-in local safe MCP wire audit. No request body, owner, URL or key fields."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path


class SafeWireAudit:
    def __init__(self, path: Path):
        if not path.is_absolute() or not path.parent.is_dir():
            raise ValueError("private absolute audit path required")
        parent = path.parent.stat()
        if (parent.st_mode & 0o077) != 0 or parent.st_uid != os.getuid():
            raise ValueError("audit directory must be private and owned")
        self.path = path

    def __call__(self, method: str, reason: str) -> None:
        # Called only after an allowlist scrub in EventASGI.
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": method,
            "result": reason,
        }
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("invalid audit file security")
            # Prevent unbounded growth in the owner-only diagnostic canary.
            if info.st_size > 131072:
                os.ftruncate(fd, 0)
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
