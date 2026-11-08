"""Deployment invariants. The Vitals source only stages; it never sends to Work."""
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_source_only_unit_uses_current_haru_home_and_network_loopback_only():
    u=(ROOT/'deploy/systemd/haru-vitals-source.service').read_text()
    assert 'Environment=HOME=/var/lib/haru-life-events' in u
    assert u.rstrip().count('-m scripts.vitals_source') == 1
    assert 'WORK_DELIVERY' not in u
    assert 'Environment=HARU_VITALS_SOURCE_ENABLED=1' in u
    assert 'IPAddressDeny=any' in u
    assert 'IPAddressAllow=127.0.0.0/8' in u
    assert 'ReadWritePaths=/var/lib/haru-life-events/.local/share/haru-life-events' in u
    assert 'User=haru' in u
    assert 'haru-life-events-location-delivery' not in u

def test_script_is_explicit_opt_in_and_source_only():
    sh=(ROOT/'deploy/enable-vitals-source.sh').read_text()
    assert 'systemctl enable --now "$unit"' in sh
    assert 'systemctl disable --now "$unit"' in sh
    assert "vitals-outbox?limit=1" in sh
    assert '-m scripts.vitals_source$' in sh
    assert 'life.location.changed' not in sh
    assert 'haru-life-events-location-delivery.service' not in sh


def test_context_worker_fails_fast_without_media_root(monkeypatch):
    """A misconfigured unit must exit (systemd sees it), not loop 'active' with no delivery."""
    import pytest
    from scripts import telegram_work_delivery as worker
    monkeypatch.setenv('HARU_LOCATION_WORKER_ENABLED', '1')
    monkeypatch.setenv('HARU_EVENTS_OWNER', 'owner-test')
    monkeypatch.delenv('HARU_TELEGRAM_MEDIA_ROOT', raising=False)
    monkeypatch.setattr(worker.time, 'sleep', lambda *_: (_ for _ in ()).throw(AssertionError('entered the loop')))
    with pytest.raises(SystemExit, match='config error'):
        worker.main()


def test_vitals_source_fails_fast_without_owner(monkeypatch):
    import pytest
    from scripts import vitals_source
    monkeypatch.setenv('HARU_VITALS_SOURCE_ENABLED', '1')
    monkeypatch.delenv('HARU_EVENTS_OWNER', raising=False)
    monkeypatch.setattr(vitals_source.time, 'sleep', lambda *_: (_ for _ in ()).throw(AssertionError('entered the loop')))
    with pytest.raises(SystemExit, match='config error'):
        vitals_source.main()


def test_media_root_drop_in_matches_the_worker_setting():
    conf = (ROOT/'deploy/systemd/haru-life-events-location-delivery.service.d/10-media-root.conf').read_text()
    assert 'Environment=HARU_TELEGRAM_MEDIA_ROOT=/' in conf


def test_queued_event_of_removed_type_is_dropped_not_stuck(tmp_path):
    import base64, json, sqlite3
    from datetime import datetime, timezone
    from cryptography.fernet import Fernet
    from haru_life_events.event_bridge_v2 import EventSubscriptions
    from haru_life_events.event_delivery_v2 import EventDeliveryQueue
    class Verified:
        def verify(self, *a): pass
    t = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
    store = EventSubscriptions(tmp_path/'s.sqlite', encryption_key=Fernet.generate_key(),
                               identity_key=b'q'*32, verifier=Verified())
    store.subscribe('owner-test', {'name': 'life.location.changed', 'arguments': {},
                    'delivery': {'mode': 'webhook', 'url': 'https://events.example.org/x',
                                 'secret': 'whsec_'+base64.b64encode(b'k'*32).decode()}}, t)
    queue = EventDeliveryQueue(tmp_path/'d.sqlite', store)
    sid = store.eligible('owner-test', 'life.location.changed', t)[0].id
    legacy = {'eventId': 'evt_legacy001', 'name': 'life.vitals.trend_changed', 'timestamp': '2026-10-12T09:00:00Z',
              'data': {'trend': 'sleep_below_baseline'}, 'cursor': None}
    with sqlite3.connect(tmp_path/'d.sqlite') as db:
        db.execute("INSERT INTO event_deliveries(owner,subscription_id,event_id,envelope,state,attempts,next_at)"
                   " VALUES (?,?,?,?, 'pending', 0, ?)", ('owner-test', sid, 'evt_legacy001', json.dumps(legacy), '2026-10-12T08:00:00Z'))
    class Never:
        def deliver(self, **kw): raise AssertionError('must not send a removed event type')
    assert queue.drain_one(owner='owner-test', now=t, sender=Never()) != 'sent'
    assert queue.drain_one(owner='owner-test', now=t, sender=Never()) == 'empty'


def _no_loop(monkeypatch, module):
    monkeypatch.setattr(module.time, 'sleep', lambda *_: (_ for _ in ()).throw(AssertionError('entered the loop')))


def _good_home(tmp_path, monkeypatch, *, keys=True):
    home = tmp_path/'home'
    (home/'.local/share/haru-life-events').mkdir(parents=True)
    (home/'.config/haru-life-events').mkdir(parents=True)
    if keys:
        from cryptography.fernet import Fernet
        for name, value in (('vault.key', Fernet.generate_key()), ('identity.key', b'i'*32)):
            key = home/'.config/haru-life-events'/name
            key.write_bytes(value)
            key.chmod(0o600)
    media = tmp_path/'media'
    media.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HARU_EVENTS_OWNER', 'owner-test')
    monkeypatch.setenv('HARU_TELEGRAM_MEDIA_ROOT', str(media))
    return home, media


import pytest as _pytest


@_pytest.mark.parametrize('url', [
    'http://127.0.0.1:bad/v1/health/vitals-outbox',
    'http://127.0.0.1:65536/v1/health/vitals-outbox',
    'http://127.0.0.1:0/v1/health/vitals-outbox',
    'http://[::1/v1/health/vitals-outbox',
])
def test_vitals_source_rejects_bad_health_url_at_startup(tmp_path, monkeypatch, url):
    from scripts import vitals_source
    _good_home(tmp_path, monkeypatch)
    monkeypatch.setenv('HARU_VITALS_SOURCE_ENABLED', '1')
    monkeypatch.setenv('HARU_VITALS_HEALTH_URL', url)
    _no_loop(monkeypatch, vitals_source)
    with _pytest.raises(SystemExit, match='config error'):
        vitals_source.main()


def test_vitals_source_rejects_missing_state_dir(tmp_path, monkeypatch):
    from scripts import vitals_source
    monkeypatch.setenv('HOME', str(tmp_path/'nohome'))
    monkeypatch.setenv('HARU_EVENTS_OWNER', 'owner-test')
    monkeypatch.setenv('HARU_VITALS_SOURCE_ENABLED', '1')
    monkeypatch.delenv('HARU_VITALS_HEALTH_URL', raising=False)
    _no_loop(monkeypatch, vitals_source)
    with _pytest.raises(SystemExit, match='config error'):
        vitals_source.main()


@_pytest.mark.parametrize('breakage', ['missing_media_dir', 'relative_media', 'missing_keys', 'missing_state'])
def test_context_worker_rejects_each_bad_input_at_startup(tmp_path, monkeypatch, breakage):
    import shutil
    from scripts import telegram_work_delivery as worker
    home, media = _good_home(tmp_path, monkeypatch, keys=(breakage != 'missing_keys'))
    monkeypatch.setenv('HARU_LOCATION_WORKER_ENABLED', '1')
    if breakage == 'missing_media_dir':
        monkeypatch.setenv('HARU_TELEGRAM_MEDIA_ROOT', str(tmp_path/'does-not-exist'))
    elif breakage == 'relative_media':
        monkeypatch.setenv('HARU_TELEGRAM_MEDIA_ROOT', 'media')
    elif breakage == 'missing_state':
        shutil.rmtree(home/'.local/share/haru-life-events')
    _no_loop(monkeypatch, worker)
    with _pytest.raises(SystemExit, match='config error'):
        worker.main()


def test_valid_config_passes_preflight(tmp_path, monkeypatch):
    from scripts import telegram_work_delivery as worker, vitals_source
    home, media = _good_home(tmp_path, monkeypatch)
    monkeypatch.delenv('HARU_VITALS_HEALTH_URL', raising=False)
    assert worker.preflight()[1] == media
    assert vitals_source.preflight() == home/'.local/share/haru-life-events'
