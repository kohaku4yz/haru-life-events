"""Telegram location-change detector, using the bundled Japan reverse geocoder.

Rules: the first explicitly shared location emits a location change; subsequent
live edits emit only after >=2km from the LAST EMITTED coordinate. There are
no named zones, quiet hours, debounce windows or notification cooldowns.

Coordinates remain in a private local SQLite state for durable distance
comparison. Candidates contain only geocoded place and observation timestamp.
This module does not poll Telegram or deliver events.
"""
from __future__ import annotations

import hashlib
import math
import os
import stat
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .life_events import Candidate
from .reverse_geocoder import JapanReverseGeocoder, ResolvedPlace

MIN_MOVEMENT_M = 2000.0
_EARTH_RADIUS_M = 6_371_000.0


def _coord(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    f = float(value)
    return f if math.isfinite(f) and low <= f <= high else None


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    a = math.radians(a_lat)
    b = math.radians(b_lat)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(b_lon-a_lon)/2)**2
    return _EARTH_RADIUS_M * 2 * math.asin(min(1.0,math.sqrt(max(0.0,h))))


def _tighten_private_state(path: str) -> None:
    """Fail closed for foreign/symlink state; tighten owner-owned SQLite to 0600.

    systemd's inherited UMask may create a 0640 DB on first integration.
    Refuse to use it until owner-read-only permissions are in place.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("invalid location state owner or file type")
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o600)
        if os.fstat(fd).st_mode & 0o077:
            raise ValueError("location state database is not private")
    finally:
        os.close(fd)


class HaruTelegramLocationAdapter:
    """Owner-only adapter using durable last-triggered-position state.

    Requires one existing getUpdates consumer (your bot's poller) and uses
    the bundled JapanReverseGeocoder.
    """

    def __init__(self, *, allowed_user_id: int, allowed_chat_id: int,
                 sqlite_path: str | Path,
                 place_resolver: Callable[[float,float],ResolvedPlace | None] | None = None) -> None:
        if _whole(allowed_user_id) is None or _whole(allowed_chat_id) is None:
            raise ValueError("owner and private chat IDs must be integers")
        self.user_id = allowed_user_id
        self.chat_id = allowed_chat_id
        self.path = str(sqlite_path)
        self.resolve = place_resolver or JapanReverseGeocoder().resolve
        parent = Path(self.path).parent
        if not parent.exists():
            raise ValueError("private location state parent missing")
        _tighten_private_state(self.path)
        old_umask = os.umask(0o077)
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS location_shares (
                        message_id INTEGER PRIMARY KEY,
                        anchor_lat REAL NOT NULL, anchor_lon REAL NOT NULL,
                        anchor_timestamp INTEGER NOT NULL,
                        last_update_id INTEGER NOT NULL, active INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL DEFAULT 0
                    );
                """)
                cols={row[1] for row in db.execute("PRAGMA table_info(location_shares)")}
                if "expires_at" not in cols:
                    db.execute("ALTER TABLE location_shares ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0")
                db.commit()
        finally:
            os.umask(old_umask)
        _tighten_private_state(self.path)

    @staticmethod
    def _candidate(update_id: int, message_id: int, at: datetime,
                   place: str, reason: str) -> Candidate:
        eid="tg_"+hashlib.sha256(
            f"{update_id}:{message_id}:life.location.changed".encode()
        ).hexdigest()[:28]
        at_iso=at.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
        return Candidate("life.location.changed",eid,at,
                         {"place":place, "observed_at":at_iso, "reason":reason})

    def observe(self, update: dict[str,Any], *, now: datetime) -> tuple[Candidate,...]:
        if not isinstance(update,dict) or not isinstance(now,datetime) or now.tzinfo is None:
            return ()
        update_id=_whole(update.get("update_id"))
        if update_id is None or update_id<0:
            return ()
        edited=isinstance(update.get("edited_message"),dict)
        msg=update.get("edited_message") if edited else update.get("message")
        if not isinstance(msg,dict):
            return ()
        sender=msg.get("from") or {}
        chat=msg.get("chat") or {}
        if not isinstance(sender,dict) or not isinstance(chat,dict):
            return ()
        if _whole(sender.get("id"))!=self.user_id or _whole(chat.get("id"))!=self.chat_id:
            return ()
        loc=msg.get("location")
        mid=_whole(msg.get("message_id"))
        if not isinstance(loc,dict) or mid is None or mid<0:
            return ()
        lat=_coord(loc.get("latitude"),-90,90)
        lon=_coord(loc.get("longitude"),-180,180)
        at_ts=_whole(msg.get("edit_date") if edited else msg.get("date"))
        if lat is None or lon is None or at_ts is None or at_ts<0:
            return ()
        at=datetime.fromtimestamp(at_ts,tz=timezone.utc)
        if at>now.astimezone(timezone.utc).replace(microsecond=0):
            # Future timestamps are untrusted, never send or move the anchor.
            return ()
        live_period=_whole(loc.get("live_period"))
        live=live_period is not None and live_period>0
        with closing(sqlite3.connect(self.path,timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            previous=db.execute(
                "SELECT anchor_lat,anchor_lon,anchor_timestamp,last_update_id,active "
                "FROM location_shares WHERE message_id=?", (mid,)
            ).fetchone()
            if previous is not None and update_id<=previous[3]:
                db.rollback()
                return ()
            if edited and not live:
                if previous is not None:
                    db.execute("UPDATE location_shares SET active=0,anchor_lat=0,anchor_lon=0,last_update_id=? WHERE message_id=?",
                               (update_id,mid))
                    db.commit()
                else:
                    db.rollback()
                return ()
            if edited and previous is None:
                # Cannot claim a new sharing session from an edit alone.
                db.rollback()
                return ()
            if edited and previous is not None and not previous[4]:
                db.rollback()
                return ()
            if live and live_period != 0x7FFFFFFF:
                sent_at=_whole(msg.get("date"))
                if sent_at is None or now.timestamp()>=sent_at+live_period:
                    db.rollback()
                    return ()
            if previous is not None and at_ts<=previous[2]:
                db.rollback()
                return ()
            if previous is None:
                reason="sharing_started"
            elif distance_m(previous[0],previous[1],lat,lon)>=MIN_MOVEMENT_M:
                reason="distance_2km"
            else:
                # Update the cursor, NOT the position anchor. The comparison
                # continues relative to the last notification, not each poll.
                db.execute("UPDATE location_shares SET last_update_id=? WHERE message_id=?",
                           (update_id,mid))
                db.commit()
                return ()
            try:
                place=self.resolve(lat,lon)
            except (OSError,ValueError):
                place=None
            label=place.label.strip() if place and isinstance(place.label,str) else ""
            if not label or len(label)>120 or any(ord(c)<32 for c in label):
                # No made-up city names or raw coordinates if geocoding fails.
                db.rollback()
                return ()
            candidate=self._candidate(update_id,mid,at,label,reason)
            db.execute("""
                INSERT INTO location_shares(message_id,anchor_lat,anchor_lon,
                    anchor_timestamp,last_update_id,active,expires_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET
                    anchor_lat=excluded.anchor_lat,anchor_lon=excluded.anchor_lon,
                    anchor_timestamp=excluded.anchor_timestamp,
                    last_update_id=excluded.last_update_id,active=excluded.active,
                    expires_at=excluded.expires_at
            """,(mid,lat if live else 0.0,lon if live else 0.0,at_ts,update_id,1 if live else 0,
                 int(msg["date"])+live_period if live and live_period != 0x7FFFFFFF else 0))
            db.commit()
        return (candidate,)


__all__=["HaruTelegramLocationAdapter","distance_m","MIN_MOVEMENT_M"]
