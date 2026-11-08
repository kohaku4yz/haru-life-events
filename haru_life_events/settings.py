"""Deployment settings read from the environment, validated fail-closed.

Nothing personal is hard-coded: the owner principal, the loopback health
outbox URL and the Telegram media root all come from the service unit.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

_OWNER = re.compile(r"^[A-Za-z0-9_-]{3,64}$")  # same rule as prepare_canary
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_HEALTH_URL = "http://127.0.0.1:8768/v1/health/vitals-outbox?limit=100"


def valid_owner(owner: object) -> bool:
    return isinstance(owner, str) and bool(_OWNER.fullmatch(owner))


def events_owner() -> str:
    """The single owner principal every subscription and delivery belongs to."""
    owner = os.getenv("HARU_EVENTS_OWNER", "")
    if not valid_owner(owner):
        raise RuntimeError("HARU_EVENTS_OWNER is missing or malformed")
    return owner


def health_outbox_url() -> str:
    """Loopback-only plain-HTTP Vitals outbox; no proxies, no remote hosts."""
    url = os.getenv("HARU_VITALS_HEALTH_URL", DEFAULT_HEALTH_URL)
    try:
        parts = urlsplit(url)
        port = parts.port  # raises on a non-numeric or out-of-range port
    except ValueError:
        raise RuntimeError("HARU_VITALS_HEALTH_URL is not a valid URL") from None
    if (parts.scheme != "http" or parts.hostname not in _LOOPBACK or not port or parts.username
            or parts.password or parts.fragment or not parts.path.endswith("/v1/health/vitals-outbox")):
        raise RuntimeError("HARU_VITALS_HEALTH_URL must be a loopback http .../v1/health/vitals-outbox URL")
    return url


def telegram_media_root() -> Path:
    """Directory of the existing Telegram inbox that records owner location shares."""
    raw = os.getenv("HARU_TELEGRAM_MEDIA_ROOT", "")
    if not raw or not os.path.isabs(raw):
        raise RuntimeError("HARU_TELEGRAM_MEDIA_ROOT must be an absolute path")
    if not os.path.isdir(raw):
        raise RuntimeError("HARU_TELEGRAM_MEDIA_ROOT is not an existing directory")
    return Path(raw)


def events_state_dir() -> Path:
    """Private state of the events services (gates, queue, subscriptions)."""
    state = Path.home() / ".local/share/haru-life-events"
    if not state.is_dir():
        raise RuntimeError("events state directory is missing")
    return state


__all__ = ["valid_owner", "events_owner", "health_outbox_url", "telegram_media_root", "events_state_dir",
           "DEFAULT_HEALTH_URL"]
