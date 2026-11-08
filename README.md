# Haru Life Events

> A small MCP Events service that wakes your AI agent when your life context changes: you moved somewhere new, or your body changed state (started walking, woke up, heart rate rose).

📖 **Guide (中文 / English):** https://kohaku4yz.github.io/haru-life-events/

```
Telegram live location → your bot's poller + opt-in hook (first share / ≥ 2 km) ──┐
Apple Watch → PulsHealth → pulse-relay v0.2 Vitals extension → loopback outbox ───┤
                                                                                  ▼
          context worker → life.context.changed → signed webhook → ChatGPT Work task
Events MCP backend (events/list · subscribe) ←── private OpenAI Tunnel ──→ ChatGPT
```

- **One event.** Location and Vitals both arrive as `life.context.changed` (`kind: location | vitals`), so the agent needs only one subscription.
- **Location:** a fuzzy place label like `丸の内, 千代田区, 東京都` from a Japan-only reverse geocoder. The agent never gets coordinates.
- **Vitals:** triggers like `movement_started` or `woke_up`, plus a small snapshot (latest heart rate, steps in the last hour…). Detection lives in [pulse-relay](https://github.com/kohaku4yz/pulse-relay) v0.2; this repo only reads its loopback outbox.
- **Reliability:** signed webhooks (HMAC-SHA256), bounded retries, a maximum age per event (Vitals 90 min, location 3 h) so nothing arrives late, and settings that fail closed.
- About 3k lines of Python. The only runtime dependency is `cryptography` (plus `uvicorn` for the backend).

Architecture, event contract, routing and failure behaviour: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- A Linux host with systemd, git and Python ≥ 3.11 with `venv`.
- ChatGPT with event-triggered scheduled tasks (Plus or above). You subscribe from a **Work** chat; each subscription uses one scheduled-task slot.
- An OpenAI Tunnel ID and runtime key, with `tunnel-client` installed at `/opt/tunnel-client/tunnel-client`.
- For Vitals: [pulse-relay](https://github.com/kohaku4yz/pulse-relay) ≥ 0.2 with its Vitals extension, on the same host.
- For location: a Telegram bot whose poller you control. The hook runs inside that poller; never start a second poller for the same bot.

## Quick start

The units expect the service user `haru`, the checkout in `/opt/haru-life-events` and the owner label `my-agent`. To use another owner label, change `HARU_EVENTS_OWNER` in `deploy/systemd/*` and commit it in your clone (`enable-vitals-source.sh` refuses uncommitted changes).

```bash
# 1. Service user, checkout, venv
sudo useradd --system --create-home --home-dir /var/lib/haru-life-events haru
sudo git clone https://github.com/kohaku4yz/haru-life-events /opt/haru-life-events
sudo python3 -m venv /opt/haru-life-events/.venv
sudo /opt/haru-life-events/.venv/bin/pip install -e '/opt/haru-life-events[server]'

# 2. Private Tunnel config as the service user (0600 secret files; nothing starts yet)
cd /opt/haru-life-events
TUNNEL_ID=tunnel_...   # your Tunnel ID: "tunnel_" + 32 hex characters
sudo -u haru -H .venv/bin/python -m haru_life_events.prepare_canary \
  --tunnel-id "$TUNNEL_ID" --owner my-agent \
  --config-dir /var/lib/haru-life-events/.config/haru-life-events \
  --data-dir /var/lib/haru-life-events/.local/share/haru-life-events

# 3. Runtime key (interactive, hidden input), then the Events MCP backend
sudo bash deploy/owner-install-runtime-key.sh
sudo bash deploy/systemd/owner-install-events-service.sh --install

# 4. Context worker (needed for both location and Vitals)
sudo install -d -o haru -g haru -m 0700 /var/lib/telegram-media
sudo install -D -m 0644 deploy/systemd/haru-life-events-location-delivery.service.d/10-media-root.conf \
  /etc/systemd/system/haru-life-events-location-delivery.service.d/10-media-root.conf
sudo install -m 0644 deploy/systemd/haru-life-events-location-delivery.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now haru-life-events-location-delivery

# 5. Vitals source (checks pulse-relay's loopback outbox first)
sudo bash deploy/enable-vitals-source.sh --install
```

**Location (optional).** Wire the hook into your poller, using `deploy/telegram/media-location-optin.patch` as an example. Build the hook once with `build_hook_from_config(owner_id=<your Telegram user id>, root=<media root>, env=os.environ)`, also ask Telegram for `edited_message` updates (live location arrives as edits), and call `observe_update()` for every update. Give the poller `PYTHONPATH=/opt/haru-life-events` and `HARU_LOCATION_EVENTS_ENABLED=1`. Run the poller as `haru`, the same user as the context worker: both open the location gate (a 0600 SQLite file in the media root, `/var/lib/telegram-media` in the units), and a file owned by another user is refused.

**Subscribe from ChatGPT.**

1. Add the Tunnel's MCP server to ChatGPT. The [haru-vps-mcp guide](https://kohaku4yz.github.io/haru-vps-mcp/) walks through private Tunnels.
2. In a Work chat, ask something like "@Life Events can you subscribe to the `life.context.changed` event?". ChatGPT verifies the callback and writes the task prompt. You can edit the prompt from the event chip.
3. Turn on the unified route. `arm` asks you to confirm that the Work task is bound to this subscription. On a fresh install, set the flag once ChatGPT shows the subscription:

```bash
cd /opt/haru-life-events
sudo -u haru -H env HARU_EVENTS_OWNER=my-agent HARU_CONTEXT_WORK_CANARY_RUN_VERIFIED=1 \
  .venv/bin/python -m scripts.context_cutover arm
sudo -u haru -H env HARU_EVENTS_OWNER=my-agent .venv/bin/python -m scripts.context_cutover status
# expect UNIFIED_ROUTING_ACTIVE=true and UNIFIED_CONTEXT_SUBSCRIBERS=1 (exit code 3 = routing off)
```

ChatGPT refreshes the subscription before it expires (this server: 7 days by default, at most 30).

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[server,test]'
.venv/bin/python -m pytest -q
```

`scripts/smoke_puls_work_pipeline.py` is an offline two-repo contract test: it seeds a synthetic health sample and Vitals candidate, reads them through pulse-relay's real outbox reader, then stages and routes one `life.context.changed` event through the delivery queue to a stub sender (no network; webhook signing is covered by the unit tests). It needs pulse-relay ≥ 0.2 installed:

```bash
.venv/bin/pip install "pulse-relay @ git+https://github.com/kohaku4yz/pulse-relay@v0.2.0"
PYTHONPATH=. .venv/bin/python scripts/smoke_puls_work_pipeline.py
```

## Notes

- iOS decides when PulsHealth syncs, and HealthKit data is locked while the phone is locked, so overnight Vitals arrive after the first unlock.
- No coordinates, Telegram IDs, raw health time series or device IDs reach the agent. Coordinates go only to the reverse geocoder (HeartRails, then GSI) to look up the place label.

## License

MIT © kohaku4yz. See [THIRD-PARTY.md](THIRD-PARTY.md) for dependencies and external services.
