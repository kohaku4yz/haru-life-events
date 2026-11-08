# Architecture

Haru Life Events turns two kinds of owner-shared life signals into **one** MCP 2.0
Events notification for an AI agent's Work task: a coarse **location change** and
a confirmed **Vitals change** from PulsHealth. Both are live, through the unified
context route described under [Routing](#routing).

## Data flow

```
Telegram (owner shares live location)                Apple Watch → PulsHealth app
        │ existing inbox poller + opt-in hook                  │
        ▼                                                      ▼
telegram_location.py: first share or ≥ 2 km   PulsHealth relay (pulse-relay)
  → fuzzy place label (reverse geocoder)         vitals extension detects movement, heart-rate
        │                                        and sleep/wake changes → loopback
        ▼                                        GET /v1/health/vitals-outbox
location gate (.location-events.sqlite3)                       │
        │                                       scripts/vitals_source.py (every 60 s)
        │                                         validates + stages → vitals-gate.sqlite3
        └──────────────┬───────────────────────────────────────┘
                       ▼
     context worker (scripts/telegram_work_delivery.py, every 5 s)
       wraps each source event as life.context.changed {kind: location | vitals}
       → durable delivery queue → signed webhook → Work task wakes up
```

The **Events MCP backend** (`scripts/owner_tunnel_canary.py`, unit
`haru-life-events-canary.service`) is separate: it serves `events/list` and
subscription admission to Work over a private OpenAI Tunnel. It never sends events.

## Event contract

Work subscribes to exactly one event, `life.context.changed`, a discriminated union:

- `{"kind": "location", "location": {"place", "observed_at", "reason"}}` with
  `reason` = `sharing_started` | `distance_2km`. No coordinates, no Telegram IDs.
- `{"kind": "vitals", "vitals": {"triggers", "observed_at", "snapshot"}}` with
  triggers from `movement_started`, `movement_settled`, `heart_rate_rose`,
  `heart_rate_fell`, `sleep_started`, `woke_up`. The snapshot holds only fresh,
  bounded PulsHealth summaries, each with its own `observed_at`; stale or missing
  metrics are omitted, never shown as zero.

`life.location.changed` remains in the catalog for the location-only fallback
route (see Routing). There are no other event types.

Each delivered event keeps its source's stable `eventId`, so replays never
notify twice. A notification is only useful while it's recent, so each event
has a maximum age: **90 minutes for Vitals, 3 hours for location**. The cap is
checked twice: when an event moves into the delivery queue, and again before
every send. Older events, or events with an unreadable timestamp, are dropped
and the journal shows each kind differently:

- dropped at transfer: `location_delivery expired=N` (N = events dropped
  in that loop);
- dropped at send time (already queued): queue outcome `expired`, logged as
  `location_delivery copied=N outcome=expired` (N = events moved into the
  queue in the same loop, often 0).

Search for both when checking for drops. Worker downtime or a routing
fallback therefore never turns into a late burst.

## Routing

The context worker uses the unified route (location **and** Vitals as
`life.context.changed`) only when both hold:

1. the owner armed it: `~/.local/share/haru-life-events/.unified-context.enabled`
   exists (created by `scripts/context_cutover.py arm`; owner-only file, mode 0600);
2. there is **exactly one** unexpired `life.context.changed` subscription.
   Subscriptions expire (7 days by default on this server, at most 30); ChatGPT refreshes them before they run out.

Otherwise it falls back to the location-only route. Every route change is
logged as `location_delivery route=unified|fallback`, including the first loop
after start.

- **Location** goes out as `life.location.changed` if such a subscription exists.
  If none does, location events stay pending in their gate until a route has a
  subscriber again; any that pass 3 hours while waiting are dropped.
- **Vitals** are not delivered on this route. They stay staged, and while any are
  staged the worker logs `location_delivery route=fallback vitals_unread=N` right away, then
  hourly (`100+` when the bounded sample is full). Once the unified route
  returns, anything older than 90 minutes is dropped.

Check the routing state as the service user, with the unit's `HOME` and
`HARU_EVENTS_OWNER` (the marker must belong to that user):
`python -m scripts.context_cutover status`. Expect `UNIFIED_ROUTING_ACTIVE=true`
and `UNIFIED_CONTEXT_SUBSCRIBERS=1`. The command exits 3 when unified routing is
off, so a health check can use the exit code alone.

## Location trigger

- The first live-location share sends one event; during sharing, another event
  follows each time the distance from the last **emitted** position reaches 2 km.
- The anchor is persisted (mode 0600), so restarts don't reset it, and cleared
  when sharing stops or a timed share expires.
- Place labels come from a Japan-only reverse geocoder (HeartRails, then GSI).
  A failed lookup never invents a place.
- The hook runs inside the existing Telegram inbox poller. Never start a second poller.

## Vitals

Detection lives in the PulsHealth relay's vitals extension, next to the health
data (see its `ALGORITHM.md`). This service only reads the relay's loopback
outbox, re-validates every field and timestamp, and stages accepted candidates.
It never opens the health database.

## Configuration

| Variable | Used by | Meaning |
|---|---|---|
| `HARU_EVENTS_OWNER` | both workers, operator scripts | owner principal of all subscriptions (required) |
| `HARU_TELEGRAM_MEDIA_ROOT` | context worker | existing Telegram inbox directory (required, must exist) |
| `HARU_LOCATION_WORKER_ENABLED=1` | context worker | explicit opt-in |
| `HARU_LOCATION_EVENTS_ENABLED=1` | Telegram inbox hook | explicit opt-in for capturing location shares |
| `HARU_VITALS_SOURCE_ENABLED=1` | Vitals source | explicit opt-in |
| `HARU_VITALS_HEALTH_URL` | Vitals source | loopback-only outbox URL (default `http://127.0.0.1:8768/v1/health/vitals-outbox?limit=100`) |

Private state lives in `~/.local/share/haru-life-events/` (gates, queue,
subscriptions) and keys in `~/.config/haru-life-events/`.

## Failure behaviour

- Both workers check their environment settings and state directory (the context
  worker also its key files) before the loop and **exit non-zero** on a bad value, so systemd shows a failed
  unit. The context worker also checks the routing marker (if present, it must be
  well-formed) and trial-decrypts the stored subscriptions for both routes, so a
  wrong vault key fails the unit instead of looping "active" with nothing
  delivered. Transient outages (health outbox down, webhook errors) stay
  retryable inside the loop.
- Stale events are dropped, not sent late (see Event contract).
- Webhook delivery retries with backoff, re-checks the subscription before every
  attempt, and gives up after a bounded number of attempts.

## Deploy order

1. Install unit drop-ins (e.g. `deploy/systemd/haru-life-events-location-delivery.service.d/`)
   and `systemctl daemon-reload` **before** moving the checkout.
2. Update the checkout.
3. Restart the long-running processes that import the changed code: the Events
   MCP backend (`haru-life-events-canary`, historical name), the context worker
   (`haru-life-events-location-delivery`) and the Vitals source
   (`haru-vitals-source`). Your Telegram poller also
   imports this checkout (`telegram_location.py`, `telegram_media_hook.py`,
   `life_events.py`, the geocoder): restart it when those change.
4. Verify the restarted units are active, `events/list` shows the expected catalog,
   `context_cutover status` exits 0 with `UNIFIED_ROUTING_ACTIVE=true` and one
   context subscriber, and the context worker's journal shows `route=unified`.

## Tests

    python3 -m venv .venv && .venv/bin/pip install -e '.[server,test]'
    .venv/bin/python -m pytest -q

`scripts/smoke_puls_work_pipeline.py` is an offline two-repo contract test: a synthetic
health sample and Vitals candidate go through pulse-relay's real outbox reader, then
one context event is staged and routed through the delivery queue to a stub sender
(no network; webhook signing is covered by the unit tests).
