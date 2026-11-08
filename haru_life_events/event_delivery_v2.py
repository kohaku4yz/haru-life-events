"""Durable, bounded, owner-scoped MCP Events delivery queue; no scheduler.

The caller must authenticate the owner when enqueueing. The worker re-checks
active subscription eligibility for each attempt. Sending is explicitly
opt-in; nothing here starts a daemon or connects to Work on import.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .location_payload import location_data_valid
from .context_payload import CONTEXT_NAME, valid_context_data
from .event_bridge_v2 import (
    ALLOWED_EVENT_FIELDS, CallbackError, EventSubscriptions,
    PinnedWebhookClient, _iso, _utc,
)

MAX_ATTEMPTS = 6
# A life-context notification is only useful while it's recent. Older events are
# dropped (outcome "expired") at transfer AND at send time, so worker downtime or a
# routing fallback never turns into a burst of stale wake-ups.
VITALS_MAX_AGE = timedelta(minutes=90)
LOCATION_MAX_AGE = timedelta(hours=3)


def event_max_age(event: dict[str, Any]) -> timedelta:
    data = event.get("data")
    kind = data.get("kind") if event.get("name") == CONTEXT_NAME and isinstance(data, dict) else "location"
    return VITALS_MAX_AGE if kind == "vitals" else LOCATION_MAX_AGE


def event_age(event: dict[str, Any], now: datetime) -> timedelta | None:
    try:
        stamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
        return _utc(now) - _utc(stamp)
    except (KeyError, TypeError, ValueError):
        return None


EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def validate_event(event: object) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != {
        "eventId", "name", "timestamp", "data", "cursor"
    }:
        raise ValueError("invalid event envelope")
    name, data = event.get("name"), event.get("data")
    if not isinstance(name, str) or name not in ALLOWED_EVENT_FIELDS:
        raise ValueError("unsupported event")
    if not isinstance(data, dict):
        raise ValueError("invalid event data")
    if name == CONTEXT_NAME:
        if not valid_context_data(data):
            raise ValueError("unsafe context event metadata")
    elif not location_data_valid(data):
        raise ValueError("unsafe location event metadata")
    if not isinstance(event["eventId"], str) or not EVENT_ID.fullmatch(event["eventId"]):
        raise ValueError("invalid event id")
    if event["cursor"] is not None:
        raise ValueError("replay cursor not supported")
    stamp = event["timestamp"]
    if not isinstance(stamp, str):
        raise ValueError("missing timestamp")
    try:
        sample_time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        _utc(sample_time)
    except (ValueError, TypeError):
        raise ValueError("invalid event timestamp") from None
    if len(json.dumps(event, separators=(",", ":")).encode()) > 256*1024:
        raise ValueError("oversized event")
    return event


class WebhookSender(Protocol):
    def deliver(self, *, url: str, secret: bytes, sid: str,
                event: dict[str, Any], now: datetime) -> int: ...


class EventDeliveryQueue:
    """One-shot workers may call drain_one repeatedly.

    Supports crash recovery via leases. Race-free claims across SQLite
    connections and stable event IDs; success/410/413 are terminal.
    """

    def __init__(self, path: str | Path, store: EventSubscriptions):
        self.path = str(path)
        self.store = store
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS event_deliveries(
                    owner TEXT NOT NULL, subscription_id TEXT NOT NULL,
                    event_id TEXT NOT NULL, envelope TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_at TEXT NOT NULL, lease_until TEXT,
                    PRIMARY KEY(subscription_id,event_id)
                );
            """)
            conn.commit()

    def enqueue(self, *, owner: str, event: dict[str, Any], now: datetime) -> int:
        validate_event(event)
        stamp = _iso(now)
        # A user account's principal is supplied by a trusted worker.
        if not isinstance(owner, str) or not owner:
            raise ValueError("missing owner")
        recipients = self.store.eligible(owner, event["name"], now)
        count = 0
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sub in recipients:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO event_deliveries"
                    "(owner,subscription_id,event_id,envelope,state,next_at)"
                    " VALUES (?,?,?,?,'pending',?)",
                    (owner, sub.id, event["eventId"],
                     json.dumps(event, separators=(",", ":"), ensure_ascii=False), stamp)
                )
                count += inserted.rowcount
            conn.commit()
        return count

    def already_enqueued(self, *, owner: str, event_id: str) -> bool:
        """Safe replay check after cross-database copy/ack interruption."""
        if not owner or not EVENT_ID.fullmatch(event_id):
            raise ValueError("invalid delivery identity")
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT 1 FROM event_deliveries WHERE owner=? AND event_id=? LIMIT 1",
                (owner,event_id)
            ).fetchone() is not None

    def drain_one(self, *, owner: str, now: datetime, sender: WebhookSender | None = None) -> str:
        if not isinstance(owner, str) or not owner:
            raise ValueError("missing owner")
        instant = _utc(now)
        stamp = _iso(instant)
        if sender is None:
            # Explicit no-network default; caller must opt into sender.
            return "disabled"
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT subscription_id,event_id,envelope,attempts FROM event_deliveries
                WHERE owner=? AND
                  ((state='pending' AND next_at<=?) OR
                   (state='inflight' AND lease_until<=?))
                ORDER BY next_at,rowid LIMIT 1
            """, (owner, stamp, stamp)).fetchone()
            if row is None:
                conn.rollback()
                return "empty"
            sid, eid, body, attempts = row
            conn.execute("""
                UPDATE event_deliveries SET state='inflight', lease_until=?, attempts=?
                WHERE owner=? AND subscription_id=? AND event_id=?
            """, (_iso(instant + timedelta(seconds=30)), attempts+1, owner, sid, eid))
            conn.commit()

        event = json.loads(body)
        age = event_age(event, instant)
        if age is None or age > event_max_age(event):
            with closing(sqlite3.connect(self.path, timeout=5)) as conn:
                conn.execute("""
                    UPDATE event_deliveries SET state='terminal', lease_until=NULL
                    WHERE owner=? AND subscription_id=? AND event_id=? AND state='inflight'
                """, (owner, sid, eid))
                conn.commit()
            return "expired"
        # Revoke/expiry check before every delivery (never send to an old sub).
        # A queued event whose type was removed can never be delivered: drop it
        # as permanent instead of letting it block the queue head forever.
        active = ({sub.id: sub for sub in self.store.eligible(owner, event["name"], instant)}
                  if event.get("name") in ALLOWED_EVENT_FIELDS else {})
        sub = active.get(sid)
        status = None
        if sub is not None:
            try:
                status = sender.deliver(
                    url=sub.callback, secret=sub.secret, sid=sid, event=event, now=instant
                )
            except (CallbackError, OSError, TimeoutError):
                pass
        elif sub is None:
            status = 410

        if isinstance(status, int) and 200 <= status < 300:
            state, next_at, outcome = "sent", stamp, "sent"
        elif status in (410, 413):
            state, next_at, outcome = "terminal", stamp, "permanent"
        elif attempts+1 >= MAX_ATTEMPTS:
            state, next_at, outcome = "terminal", stamp, "exhausted"
        else:
            state = "pending"
            next_at = _iso(instant + timedelta(seconds=min(3600,30 * 2**attempts)))
            outcome = "retry"
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute("""
                UPDATE event_deliveries SET state=?, next_at=?, lease_until=NULL
                WHERE owner=? AND subscription_id=? AND event_id=? AND state='inflight'
            """, (state, next_at, owner, sid, eid))
            conn.commit()
        return outcome

    def counts(self, owner: str) -> dict[str, int]:
        if not owner:
            raise ValueError("missing owner")
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT state,COUNT(*) FROM event_deliveries WHERE owner=? GROUP BY state",
                (owner,)).fetchall()
        return {key: val for key, val in rows}


__all__ = ["EventDeliveryQueue", "validate_event"]
