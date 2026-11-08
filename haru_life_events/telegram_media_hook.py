"""Opt-in Haru Telegram media-inbox location hook (no polling or network).

Call observe_update() *within* the media service's existing getUpdates loop.
Never start a second poller for that bot token. The returned count is local
diagnostic metadata; accepted events are atomically queued in SQLite and
never delivered to Work by this module.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .life_events import LifeEventGate
from .telegram_location import HaruTelegramLocationAdapter


class HaruMediaLocationHook:
    def __init__(self, *, owner_id: int, chat_id: int, sqlite_path: str | Path,
                 place_resolver=None) -> None:
        self.adapter = HaruTelegramLocationAdapter(
            allowed_user_id=owner_id, allowed_chat_id=chat_id,
            sqlite_path=sqlite_path, place_resolver=place_resolver)
        self.gate = LifeEventGate(sqlite_path)

    def observe_update(self, update: dict[str, Any], *, now: datetime) -> int:
        accepted = 0
        for event in self.adapter.observe(update, now=now):
            if self.gate.consider(event, now=now, enqueue=True).emit:
                accepted += 1
        return accepted


def build_hook_from_config(*, owner_id: int, root: str | Path,
                           env: Mapping[str, str]) -> HaruMediaLocationHook | None:
    enabled = env.get("HARU_LOCATION_EVENTS_ENABLED", "0")
    if enabled == "0":
        return None
    if enabled != "1":
        raise ValueError("invalid HARU_LOCATION_EVENTS_ENABLED")
    try:
        # The same owner already configured on the existing Haru Bot is
        # automatically the one allowed private-chat sender.
        chat_id = int(env.get("HARU_LOCATION_CHAT_ID", str(owner_id)))
    except (KeyError, ValueError) as exc:
        raise ValueError("missing private chat ID") from exc
    if chat_id != owner_id or owner_id <= 0:
        raise ValueError("expected owner-only private Telegram chat")
    return HaruMediaLocationHook(
        owner_id=owner_id, chat_id=chat_id,
        sqlite_path=Path(root) / ".location-events.sqlite3",
    )


__all__ = ["HaruMediaLocationHook", "build_hook_from_config"]
