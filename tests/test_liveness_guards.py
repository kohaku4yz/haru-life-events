"""Liveness guards: stale events never burst late, routing state is visible, bad config fails at start."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from haru_life_events.event_delivery_v2 import EventDeliveryQueue
from haru_life_events.life_events import LifeEventGate
from scripts.context_work_delivery import MARKER, transfer_context_pending
from scripts.telegram_work_delivery import transfer_pending
from tests.test_unified_context_work import T, Sender, staged, store
from tests.test_vitals_source_unit import _good_home

OWNER = 'demo-owner'


def _queued(tmp_path):
    loc, vit = staged(tmp_path)
    s = store(tmp_path)
    q = EventDeliveryQueue(tmp_path/'deliveries.sqlite', s)
    counts = transfer_context_pending(location=loc, vitals=vit, queue=q, owner=OWNER, now=T)
    assert counts['location_moved'] == 1 and counts['vitals_moved'] == 1
    return q


def test_send_time_drops_48h_old_queued_events(tmp_path):
    # Reproduces the review case: a Work outage leaves events queued for two days.
    q = _queued(tmp_path)
    send = Sender()
    later = T + timedelta(hours=48)
    assert q.drain_one(owner=OWNER, now=later, sender=send) == 'expired'
    assert q.drain_one(owner=OWNER, now=later, sender=send) == 'expired'
    assert q.drain_one(owner=OWNER, now=later, sender=send) == 'empty'
    assert send.events == []
    assert q.counts(OWNER) == {'terminal': 2}


def test_send_time_cap_is_per_kind(tmp_path):
    # At +2h Vitals (90 min cap) are stale but location (3h cap) is still worth sending.
    q = _queued(tmp_path)
    send = Sender()
    later = T + timedelta(hours=2)
    outcomes = sorted(q.drain_one(owner=OWNER, now=later, sender=send) for _ in range(2))
    assert outcomes == ['expired', 'sent']
    assert [e['data']['kind'] for e in send.events] == ['location']


def test_fresh_events_still_send(tmp_path):
    q = _queued(tmp_path)
    send = Sender()
    soon = T + timedelta(minutes=60)
    assert [q.drain_one(owner=OWNER, now=soon, sender=send) for _ in range(2)] == ['sent', 'sent']


def test_held_location_older_than_cap_is_dropped_not_burst(tmp_path):
    loc, _ = staged(tmp_path)
    q = EventDeliveryQueue(tmp_path/'deliveries.sqlite', store(tmp_path))
    expired = [0]
    assert transfer_pending(loc, q, OWNER, T + timedelta(hours=4), expired) == 0
    assert expired == [1]
    assert loc.pending() == []
    assert q.drain_one(owner=OWNER, now=T + timedelta(hours=4), sender=Sender()) == 'empty'


def test_unified_transfer_counts_expired_location(tmp_path):
    loc, vit = staged(tmp_path)
    q = EventDeliveryQueue(tmp_path/'deliveries.sqlite', store(tmp_path))
    counts = transfer_context_pending(location=loc, vitals=vit, queue=q, owner=OWNER,
                                      now=T + timedelta(hours=4))
    assert counts == {'location_moved': 0, 'vitals_moved': 0, 'vitals_expired': 1, 'location_expired': 1}
    assert loc.pending() == [] and vit.pending() == []


def _state(home):
    return home/'.local/share/haru-life-events'


def test_preflight_rejects_malformed_marker(tmp_path, monkeypatch):
    from scripts import telegram_work_delivery as worker
    home, _ = _good_home(tmp_path, monkeypatch)
    marker = _state(home)/MARKER
    marker.write_text('CONTEXT_ROUTE_V1\n')
    marker.chmod(0o644)  # must be 0600
    with pytest.raises(RuntimeError, match='routing marker or event keys'):
        worker.preflight()


def test_preflight_rejects_vault_key_that_cannot_decrypt(tmp_path, monkeypatch):
    from scripts import telegram_work_delivery as worker
    home, _ = _good_home(tmp_path, monkeypatch)
    monkeypatch.setenv('HARU_EVENTS_OWNER', OWNER)
    # Subscriptions written under a different vault key, valid at the real clock preflight uses.
    from tests import test_unified_context_work as ctx
    monkeypatch.setattr(ctx, 'T', datetime.now(timezone.utc))
    ctx.store(_state(home))
    with pytest.raises(RuntimeError, match='routing marker or event keys'):
        worker.preflight()


def test_preflight_rejects_bad_key_on_location_only_route(tmp_path, monkeypatch):
    # Before cutover only the legacy location subscription exists; a bad key must still fail at start.
    import base64
    from haru_life_events.event_bridge_v2 import EventSubscriptions
    from scripts import telegram_work_delivery as worker
    from tests.test_unified_context_work import Verifier
    home, _ = _good_home(tmp_path, monkeypatch)
    monkeypatch.setenv('HARU_EVENTS_OWNER', OWNER)
    s = EventSubscriptions(_state(home)/'subscriptions.sqlite', encryption_key=Fernet.generate_key(),
                           identity_key=b'w'*32, verifier=Verifier())
    cb = {'mode': 'webhook', 'url': 'https://events.example.com/callback',
          'secret': 'whsec_'+base64.b64encode(b't'*32).decode()}
    s.subscribe(OWNER, {'name': 'life.location.changed', 'delivery': cb}, datetime.now(timezone.utc))
    with pytest.raises(RuntimeError, match='routing marker or event keys'):
        worker.preflight()


def test_once_reports_fallback_route_and_unread_vitals(tmp_path, monkeypatch):
    from scripts import telegram_work_delivery as worker
    home, _ = _good_home(tmp_path, monkeypatch)
    monkeypatch.setenv('HARU_LOCATION_WORKER_ENABLED', '1')
    state = _state(home)
    _, vit = staged(tmp_path)
    (tmp_path/'vitals.sqlite3').rename(state/'vitals-gate.sqlite3')
    assert worker.once() == (0, 0, 'empty', 'fallback', 1)
    assert len(LifeEventGate(state/'vitals-gate.sqlite3').pending()) == 1  # fallback never consumes Vitals


def test_cutover_status_exits_nonzero_when_routing_off(tmp_path, monkeypatch, capsys):
    from scripts import context_cutover
    home, _ = _good_home(tmp_path, monkeypatch)
    monkeypatch.setattr(context_cutover, 'BASE', _state(home))
    monkeypatch.setattr(context_cutover, 'CONFIG', home/'.config/haru-life-events')
    monkeypatch.setattr('sys.argv', ['context_cutover', 'status'])
    with pytest.raises(SystemExit) as exit_info:
        context_cutover.main()
    assert exit_info.value.code == 3
    assert 'UNIFIED_ROUTING_ACTIVE=false' in capsys.readouterr().out


def test_fallback_drops_location_with_unreadable_timestamp(tmp_path):
    # Haru round 1 (pre-existing): an unreadable timestamp must not block the legacy transfer.
    import json, sqlite3
    loc, _ = staged(tmp_path)
    with sqlite3.connect(tmp_path/'location.sqlite3') as db:
        (raw,) = db.execute('SELECT envelope FROM pending_events').fetchone()
        envelope = json.loads(raw)
        envelope['timestamp'] = 'unreadable'
        db.execute('UPDATE pending_events SET envelope=?', (json.dumps(envelope),))
    q = EventDeliveryQueue(tmp_path/'deliveries.sqlite', store(tmp_path))
    expired = [0]
    assert transfer_pending(loc, q, OWNER, T, expired) == 0
    assert expired == [1]
    assert loc.pending() == []


def test_unread_label_marks_saturated_sample():
    from scripts.telegram_work_delivery import UNREAD_CAP, unread_label
    assert unread_label(1) == '1'
    assert unread_label(UNREAD_CAP - 1) == str(UNREAD_CAP - 1)
    assert unread_label(UNREAD_CAP) == f'{UNREAD_CAP}+'
