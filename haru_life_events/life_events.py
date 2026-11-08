"""Fail-closed life-event gate: validation, atomic dedup and a pending outbox.

This module neither reads health/location data nor sends anything. Trusted
sources (the Telegram location detector, the PulsHealth Vitals source) submit
sanitized candidates; the context worker moves accepted ones to delivery.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .location_payload import LOCATION_NAME, location_data_valid
from .vitals_event_payload import valid_vitals_data
from .context_payload import CONTEXT_NAME, valid_context_data

# Accepted event types. Each has a strict schema validator: location excludes GPS;
# Vitals carries only bounded recent summaries, never raw history or device IDs.
# No quiet hours or cooldowns: the sources only emit confirmed changes, and stable
# source IDs already stop repeats.
EVENT_NAMES = frozenset({LOCATION_NAME, "life.vitals.changed", CONTEXT_NAME})

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


@dataclass(frozen=True)
class Candidate:
    name: str
    source_event_id: str  # opaque stable identifier; no email address or coordinates
    occurred_at: datetime
    data: dict[str, Any]


@dataclass(frozen=True)
class Decision:
    emit: bool
    reason: str
    payload: dict[str, object] | None = None


def _validate(item: Candidate, now: datetime) -> None:
    if item.name not in EVENT_NAMES:
        raise ValueError("unsupported event type")
    if not _ID_PATTERN.fullmatch(item.source_event_id):
        raise ValueError("source_event_id must be opaque and bounded")
    if item.name == CONTEXT_NAME:
        if not valid_context_data(item.data):
            raise ValueError("unsupported or sensitive context event metadata")
    elif item.name == LOCATION_NAME:
        if not location_data_valid(item.data):
            raise ValueError("unsupported or sensitive location event metadata")
    elif not valid_vitals_data(item.data):
        raise ValueError("unsupported or sensitive Vitals event metadata")
    if item.occurred_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("timestamps must be timezone aware")
    age = now.astimezone(timezone.utc) - item.occurred_at.astimezone(timezone.utc)
    if age < timedelta(minutes=-5) or age > timedelta(hours=24):
        raise ValueError("stale or future-dated event")


class LifeEventGate:
    """SQLite-backed, fail-closed decision engine with atomic deduplication.

    The candidate must come from a separate authenticated, trusted data adapter.
    ``emit=True`` only authorizes a downstream *reviewed* delivery component;
    it does not send any webhook or start a Work task.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        with closing(sqlite3.connect(self.database_path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS processed (
                    source_event_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                -- last_emitted: kept for existing databases; no longer written (no cooldowns).
                CREATE TABLE IF NOT EXISTS last_emitted (
                    scope TEXT PRIMARY KEY,
                    emitted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_events (
                    event_id TEXT PRIMARY KEY,
                    envelope TEXT NOT NULL,
                    queued_at TEXT NOT NULL
                );
            """)
            db.commit()

    def consider(self, item: Candidate, *, now: datetime, enqueue: bool = False) -> Decision:
        """Optionally store an accepted event in the same transaction as dedup.

        Queued events are picked up by the context worker, which delivers them.
        Nothing is delivered or subscribed by this method.
        """
        _validate(item, now)
        current = now.astimezone(timezone.utc)
        iso_now = current.isoformat()
        payload: dict[str, object] = {
            "eventId": item.source_event_id,
            "name": item.name,
            "timestamp": item.occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "data": dict(item.data),
            "cursor": None,
        }
        with closing(sqlite3.connect(self.database_path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM processed WHERE source_event_id = ?", (item.source_event_id,)
            ).fetchone():
                db.rollback()
                return Decision(False, "duplicate")
            reason = "accepted"
            db.execute(
                "INSERT INTO processed (source_event_id, decision, processed_at) VALUES (?, ?, ?)",
                (item.source_event_id, reason, iso_now),
            )
            if reason == "accepted":
                if enqueue:
                    db.execute(
                        "INSERT INTO pending_events(event_id, envelope, queued_at) VALUES (?, ?, ?)",
                        (item.source_event_id, json.dumps(payload, separators=(",", ":")), iso_now),
                    )
            db.commit()
        if reason != "accepted":
            return Decision(False, reason)
        return Decision(True, reason, payload)

    def acknowledge_pending(self, event_id: str) -> bool:
        """Remove only events already durably copied to the delivery queue."""
        if not isinstance(event_id, str) or not _ID_PATTERN.fullmatch(event_id):
            raise ValueError("invalid event ID")
        with closing(sqlite3.connect(self.database_path,timeout=5)) as db:
            cursor=db.execute("DELETE FROM pending_events WHERE event_id=?", (event_id,))
            db.commit()
            return cursor.rowcount == 1

    def pending(self, *, limit: int = 20) -> list[dict[str, object]]:
        """Read only the bounded coarse pending envelopes; no network calls."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid pending limit")
        with closing(sqlite3.connect(self.database_path)) as db:
            rows = db.execute(
                "SELECT envelope FROM pending_events ORDER BY queued_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
