"""Owner-approved one-shot synthetic callback delivery.

Never accesses Telegram or Vitals. Only the coarse 'other arrived' event.
The eventId is fixed for this experiment; retries require explicit operator
review. The --send switch is required before any external webhook POST.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import os

from haru_life_events.event_bridge_v2 import EventSubscriptions, PinnedWebhookClient
from haru_life_events.settings import valid_owner
from haru_life_events.event_delivery_v2 import EventDeliveryQueue, validate_event
from haru_life_events.tunnel_events_local import _secret_file

TEST_EVENT_ID = "evt_synthetic_work_20261012_01"
SCHEMA_V2_EVENT_ID = "evt_synthetic_work_20261012_schema_v2_01"
POST_REFRESH_EVENT_ID = "evt_synthetic_work_20261012_postrefresh_01"
EVENT_NAME = "life.location.changed"


def build_envelope(now: datetime, *, event_id: str = TEST_EVENT_ID) -> dict:
    timestamp=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return validate_event({
        "eventId": event_id,
        "name": EVENT_NAME,
        "timestamp": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": {"place": "模拟区域（非真实地名）", "observed_at": timestamp, "reason": "sharing_started"},
        "cursor": None,
    })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Authorize ONE real outbound signed event")
    parser.add_argument("--schema-v2", action="store_true", help="Use the one-time refreshed schema verification ID")
    parser.add_argument("--post-refresh", action="store_true", help="Use a distinct event ID after plugin metadata Refresh")
    args = parser.parse_args()
    if args.schema_v2 and args.post_refresh:
        parser.error("choose one synthetic test ID only")
    now = datetime.now(timezone.utc)
    os.umask(0o077)
    home = Path.home()
    private_config = home / ".config/haru-life-events"
    private_state = home / ".local/share/haru-life-events"
    assert private_state.is_dir()
    owner = None
    for line in (private_config / "events.env").read_text().splitlines():
        if line.startswith("HARU_EVENTS_OWNER="):
            owner = line.split("=", 1)[1]
    if not valid_owner(owner):
        raise RuntimeError("missing or malformed configured principal")
    store = EventSubscriptions(
        private_state / "subscriptions.sqlite",
        encryption_key=_secret_file(str(private_config / "vault.key")),
        identity_key=_secret_file(str(private_config / "identity.key")),
    )
    active = store.eligible(owner, EVENT_NAME, now)
    print("EVENT_NAME=" + EVENT_NAME, flush=True)
    print("SIMULATION=true", flush=True)
    print("PLACE=synthetic_not_real", flush=True)
    print("REASON=sharing_started", flush=True)
    print("ACTIVE_SUBSCRIPTION_COUNT=" + str(len(active)), flush=True)
    if len(active) != 1:
        raise RuntimeError("requires exactly one active owner subscription")
    test_id = POST_REFRESH_EVENT_ID if args.post_refresh else SCHEMA_V2_EVENT_ID if args.schema_v2 else TEST_EVENT_ID
    payload = build_envelope(now, event_id=test_id)
    if args.schema_v2 or args.post_refresh:
        payload["data"]["place"] = "模拟地点（刷新后验收，非真实定位）" if args.post_refresh else "模拟地点（新版事件字段测试）"
        payload = validate_event(payload)
    if not args.send:
        print("DRY_RUN_ONLY=true", flush=True)
        return 0
    queue = EventDeliveryQueue(private_state / "deliveries.sqlite", store)
    inserted = queue.enqueue(owner=owner, event=payload, now=now)
    print("ENQUEUED_NEW=" + str(inserted), flush=True)
    if inserted != 1:
        print("DELIVERY_NOT_REPEATED=true", flush=True)
        return 0
    result = queue.drain_one(owner=owner, now=now, sender=PinnedWebhookClient())
    print("DELIVERY_RESULT=" + result, flush=True)
    print("QUEUE_COUNTS=" + repr(queue.counts(owner)), flush=True)
    return 0 if result == "sent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
