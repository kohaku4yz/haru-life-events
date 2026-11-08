"""Context worker: the single process that delivers life events to Work.

Moves sanitized pending location events (from the Telegram inbox hook) and, once
the unified context route is armed, staged Vitals into one durable delivery
queue, then sends signed webhooks. Requires HARU_LOCATION_WORKER_ENABLED=1,
HARU_EVENTS_OWNER and HARU_TELEGRAM_MEDIA_ROOT. Never reads raw GPS, the
Telegram bot token or raw Telegram updates.
"""
from __future__ import annotations
import os
import sqlite3
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from haru_life_events.life_events import LifeEventGate
from haru_life_events.event_delivery_v2 import EventDeliveryQueue, LOCATION_MAX_AGE, event_age
from haru_life_events.event_bridge_v2 import CallbackError, EventSubscriptions, PinnedWebhookClient
from haru_life_events.settings import events_owner, events_state_dir, telegram_media_root
from haru_life_events.tunnel_events_local import _secret_file
from haru_life_events.context_payload import CONTEXT_NAME
from scripts.context_work_delivery import context_enabled, marker_armed, transfer_context_pending

UNREAD_CAP=100  # gate.pending() maximum; monitoring never reads an unbounded outbox


def unread_label(unread: int) -> str:
    """A full sample means the backlog is at least the cap, not exactly it."""
    return f"{UNREAD_CAP}+" if unread>=UNREAD_CAP else str(unread)


def transfer_pending(gate: LifeEventGate, queue: EventDeliveryQueue,
                     owner: str, now: datetime, expired: list[int] | None = None) -> int:
    """Replay-safe transition from the private coarse outbox to Work delivery.

    Stale events are dropped; their count is added to expired[0] if given."""
    moved=0
    for event in gate.pending(limit=50):
        if event.get("name") != "life.location.changed":
            continue
        age=event_age(event,now)
        if age is None or age>LOCATION_MAX_AGE:
            # Unreadable or stale: drop, so it neither blocks the outbox nor bursts later.
            if gate.acknowledge_pending(event["eventId"]) and expired is not None:
                expired[0]+=1
            continue
        inserted=queue.enqueue(owner=owner,event=event,now=now)
        if inserted or queue.already_enqueued(owner=owner,event_id=event["eventId"]):
            gate.acknowledge_pending(event["eventId"])
            moved+=1
    return moved


def scrub_expired_anchors(path: Path, now: datetime) -> int:
    if not path.is_file():
        return 0
    with sqlite3.connect(path,timeout=5) as db:
        fields={col[1] for col in db.execute("PRAGMA table_info(location_shares)")}
        if "expires_at" not in fields:
            return 0
        changed=db.execute(
            "UPDATE location_shares SET anchor_lat=0, anchor_lon=0, active=0 "
            "WHERE active=1 AND expires_at>0 AND expires_at<=?",
            (int(now.timestamp()),)
        ).rowcount
        db.commit()
        return changed


def preflight() -> tuple[str, Path, Path, EventSubscriptions]:
    """Every configuration input, checked once; RuntimeError means a broken unit.

    Includes the routing marker (if present it must be well-formed) and a trial
    decrypt of the stored subscriptions, so a wrong vault key fails the unit instead
    of looping "active" with nothing delivered."""
    owner = events_owner()
    root = telegram_media_root()
    state = events_state_dir()
    conf = Path.home()/".config/haru-life-events"
    if not all((conf/name).is_file() for name in ("vault.key", "identity.key")):
        raise RuntimeError("events key files are missing")
    try:
        marker_armed(state)
        store = EventSubscriptions(state/"subscriptions.sqlite",
            encryption_key=_secret_file(str(conf/"vault.key")),
            identity_key=_secret_file(str(conf/"identity.key")))
        now = datetime.now(timezone.utc)
        for name in (CONTEXT_NAME, "life.location.changed"):  # both routes' secrets
            store.eligible(owner, name, now)
    except (ValueError, CallbackError, OSError) as exc:
        raise RuntimeError(f"routing marker or event keys unusable ({type(exc).__name__})") from None
    return owner, root, state, store


def once() -> tuple[int,int,str,str,int]:
    """One transfer + one delivery attempt.

    Returns (moved, expired, outcome, route, vitals_unread)."""
    if os.getenv("HARU_LOCATION_WORKER_ENABLED") != "1":
        raise RuntimeError("Telegram Work location delivery explicitly disabled")
    owner, root, state, store = preflight()
    gate=LifeEventGate(root/".location-events.sqlite3")
    queue=EventDeliveryQueue(state/"deliveries.sqlite",store)
    now=datetime.now(timezone.utc)
    scrub_expired_anchors(root/".location-events.sqlite3",now)
    vitals_path=state/'vitals-gate.sqlite3'
    vitals=LifeEventGate(vitals_path) if vitals_path.is_file() else None
    if context_enabled(state,store,owner,now):
        route='unified'
        counts=transfer_context_pending(location=gate,vitals=vitals,
                                        queue=queue,owner=owner,now=now)
        moved=counts['location_moved']+counts['vitals_moved']
        expired=counts['location_expired']+counts['vitals_expired']
        unread=0
    else:
        # Location-only fallback: Vitals are NOT delivered on this route.
        route='fallback'
        dropped=[0]
        moved=transfer_pending(gate,queue,owner,now,dropped)
        expired=dropped[0]
        unread=len(vitals.pending(limit=UNREAD_CAP)) if vitals is not None else 0
    outcome=queue.drain_one(owner=owner,now=now,sender=PinnedWebhookClient())
    return moved,expired,outcome,route,unread

def main():
    if os.getenv("HARU_LOCATION_WORKER_ENABLED")!="1":
        raise SystemExit("Location delivery disabled")
    try:
        # Fail fast: a misconfigured unit must show as failed, not loop "active" forever.
        preflight()
    except RuntimeError as exc:
        raise SystemExit(f"location_delivery config error: {exc}") from None
    closing=False
    def exit_handler(_sig,_frame):
        nonlocal closing
        closing=True
    signal.signal(signal.SIGINT,exit_handler)
    signal.signal(signal.SIGTERM,exit_handler)
    last_route=None
    last_unread_report=float('-inf')
    while not closing:
        try:
            transferred,expired,outcome,route,unread=once()
            if route!=last_route:
                # A routing change is the one thing that silently stops Vitals: always say it.
                print(f"location_delivery route={route}",flush=True)
                last_route=route
            if route=="fallback" and unread and time.monotonic()-last_unread_report>=3600:
                print(f"location_delivery route=fallback vitals_unread={unread_label(unread)}",flush=True)
                last_unread_report=time.monotonic()
            if expired:
                print(f"location_delivery expired={expired}",flush=True)
            if transferred or outcome not in ("empty","disabled"):
                print(f"location_delivery copied={transferred} outcome={outcome}",flush=True)
        except Exception as exc:
            # Never output callback URL, credentials, geocode or coordinates.
            print(f"location_delivery error={type(exc).__name__}",flush=True)
        if not closing:
            time.sleep(5)

if __name__=="__main__":main()
