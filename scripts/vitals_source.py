"""PulsHealth Vitals source worker: stage confirmed changes into the Vitals gate.

Every 60 s it reads the loopback Vitals outbox and stages new candidates in
``vitals-gate.sqlite3``. It has no Work credentials and sends nothing: the
context worker (scripts/telegram_work_delivery.py) delivers staged Vitals inside
the one shared ``life.context.changed`` event.
"""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from haru_life_events.life_events import LifeEventGate
from haru_life_events.puls_vitals_source import stage_from_health
from haru_life_events.settings import events_owner, events_state_dir, health_outbox_url


def preflight() -> Path:
    """Every configuration input, checked once; RuntimeError means a broken unit.
    A transient health-outbox outage is NOT checked here: it stays retryable."""
    events_owner()
    health_outbox_url()
    return events_state_dir()


def once() -> dict[str, int]:
    if os.getenv('HARU_VITALS_SOURCE_ENABLED') != '1':
        raise RuntimeError('Vitals source is not enabled')
    state = preflight()
    gate = LifeEventGate(state/'vitals-gate.sqlite3')
    return stage_from_health(gate, now=datetime.now(timezone.utc))


def main() -> None:
    if os.getenv('HARU_VITALS_SOURCE_ENABLED') != '1':
        raise SystemExit('Vitals source disabled')
    try:
        preflight()  # fail fast: show as failed instead of looping "active"
    except RuntimeError as exc:
        raise SystemExit(f'vitals_source config error: {exc}') from None
    ending = False

    def finish(_sig, _frame):
        nonlocal ending
        ending = True
    signal.signal(signal.SIGINT, finish)
    signal.signal(signal.SIGTERM, finish)
    while not ending:
        try:
            outcome = once()
            if outcome.get('staged'):
                print('vitals_source ' + ', '.join(f'{k}={v}' for k, v in outcome.items()), flush=True)
        except Exception as exc:
            print('vitals_source error=' + type(exc).__name__, flush=True)
        if not ending:
            time.sleep(60)


if __name__ == '__main__':
    main()
