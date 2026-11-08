"""No personal data, no outgoing sockets: fake PulsHealth source and Work sender.

Staged Vitals reach Work only through the unified context route."""
from __future__ import annotations
import base64
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from haru_life_events.puls_vitals_source import validated_candidates,stage_from_health
from haru_life_events.life_events import LifeEventGate
from haru_life_events.event_bridge_v2 import EventSubscriptions
from haru_life_events.event_delivery_v2 import EventDeliveryQueue
from haru_life_events.context_payload import CONTEXT_NAME
from scripts.context_work_delivery import transfer_context_pending

T=datetime(2026,10,12,12,tzinfo=timezone.utc)

def iso(t):return t.isoformat().replace('+00:00','Z')

def sample(*,stamp=None,triggers=None,age=10):
    at=T-timedelta(minutes=age)
    return {'status':'ok','source':'pulshealth_only','generated_at':iso(stamp or T),
            'event_name':'life.vitals.changed','candidates':[
               {'event_id':'vit_'+'a'*48,'triggers':triggers or ['woke_up','heart_rate_rose'],
                'observed_at':iso(at),'recorded_at':iso(at+timedelta(minutes=1))}],
            'snapshot':{'heart_rate_bpm':{'value':83,'observed_at':iso(T-timedelta(minutes=5)),'source':'pulshealth'},
                        'hrv_ms':{'value':45,'observed_at':iso(T-timedelta(hours=1)),'source':'pulshealth'}}}

class FakeVerifier:
 def verify(self,*args):pass

class FakeSender:
 def __init__(self):self.emitted=[]
 def deliver(self,*,event,**kwargs):self.emitted.append(event);return 202

def subscriptions(tmp_path):
    store=EventSubscriptions(tmp_path/'subscriptions.sqlite',encryption_key=Fernet.generate_key(),
                             identity_key=b'p'*32,verifier=FakeVerifier())
    cb={'mode':'webhook','url':'https://work.example.org/events',
        'secret':'whsec_'+base64.b64encode(b's'*32).decode()}
    owner='demo-owner'
    store.subscribe(owner,{'name':CONTEXT_NAME,'arguments':{},'delivery':cb},T)
    return store

def move(tmp_path,gate,queue,now=T):
    empty_location=LifeEventGate(tmp_path/'location-gate.sqlite3')
    return transfer_context_pending(location=empty_location,vitals=gate,queue=queue,owner='demo-owner',now=now)

def test_valid_source_to_one_context_event(tmp_path):
    gate=LifeEventGate(tmp_path/'vitals-gate.sqlite3')
    state=sample()
    assert stage_from_health(gate,now=T,source=state)['staged']==1
    assert stage_from_health(gate,now=T,source=state)['duplicates']==1
    store=subscriptions(tmp_path)
    queue=EventDeliveryQueue(tmp_path/'deliveries.sqlite',store)
    assert move(tmp_path,gate,queue)=={'location_moved':0,'vitals_moved':1,'vitals_expired':0,'location_expired':0}
    sender=FakeSender()
    assert queue.drain_one(owner='demo-owner',now=T,sender=sender)=='sent'
    assert len(sender.emitted)==1
    event=sender.emitted[0]
    assert event['eventId']=='vit_'+'a'*48
    assert event['name']==CONTEXT_NAME
    assert event['data']['kind']=='vitals'
    vitals=event['data']['vitals']
    assert vitals['triggers']==['woke_up','heart_rate_rose']
    assert vitals['snapshot']['heart_rate_bpm']['value']==83
    assert vitals['snapshot']['hrv_ms']['value']==45
    assert stage_from_health(gate,now=T,source=state)['staged']==0
    assert move(tmp_path,gate,queue)['vitals_moved']==0

def test_stale_backfill_does_not_stage_work_event(tmp_path):
    gate=LifeEventGate(tmp_path/'g.sqlite')
    assert stage_from_health(gate,now=T,source=sample(age=95))['staged']==0
    assert gate.pending()==[]

def test_unknown_metric_source_cannot_inject_data():
    s=sample();s['snapshot']['heart_rate_bpm']['source']='hae'
    with pytest.raises(ValueError,match='provenance'):validated_candidates(s,now=T)
    s=sample();s['snapshot']['raw_device_id']={'value':'abc','observed_at':iso(T),'source':'pulshealth'}
    with pytest.raises(ValueError,match='provenance'):validated_candidates(s,now=T)

def test_unknown_signal_and_extra_fields_refused():
    s=sample();s['candidates'][0]['triggers']=['woke_up','raw_pulse']
    with pytest.raises(ValueError):validated_candidates(s,now=T)
    s=sample();s['debug']='unused'
    with pytest.raises(ValueError):validated_candidates(s,now=T)

def test_old_source_snapshot_is_discarded_as_stale():
    s=sample(stamp=T-timedelta(minutes=20))
    with pytest.raises(ValueError):validated_candidates(s,now=T)

def test_no_subscription_does_not_ack_or_deliver(tmp_path):
    gate=LifeEventGate(tmp_path/'g.sqlite')
    stage_from_health(gate,now=T,source=sample())
    store=EventSubscriptions(tmp_path/'s.sqlite',encryption_key=Fernet.generate_key(),
                             identity_key=b'm'*32,verifier=FakeVerifier())
    queue=EventDeliveryQueue(tmp_path/'q.sqlite',store)
    assert move(tmp_path,gate,queue)['vitals_moved']==0
    assert len(gate.pending())==1
    assert queue.drain_one(owner='demo-owner',now=T,sender=None)=='disabled'

def test_expired_staged_event_is_discarded_not_backfilled(tmp_path):
    gate=LifeEventGate(tmp_path/'g.sqlite')
    stage_from_health(gate,now=T,source=sample())
    store=subscriptions(tmp_path)
    queue=EventDeliveryQueue(tmp_path/'q.sqlite',store)
    much_later=T+timedelta(minutes=100)
    assert move(tmp_path,gate,queue,much_later)['vitals_expired']==1
    assert gate.pending()==[]


def test_real_local_health_fetch_has_bounded_snapshot_timeout(monkeypatch):
    from haru_life_events import puls_vitals_source as bridge
    monkeypatch.delenv('HARU_VITALS_HEALTH_URL',raising=False)
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*_):return None
        def read(self,max_size):
            return json.dumps(sample()).encode()
    class Opener:
        def open(self,request,*,timeout):
            assert request.full_url=='http://127.0.0.1:8768/v1/health/vitals-outbox?limit=100'
            assert 4<timeout<=20
            return Response()
    monkeypatch.setattr(bridge,'build_opener',lambda *_:Opener())
    assert bridge.fetch_local_health()['status']=='ok'


def test_health_url_override_must_stay_loopback(monkeypatch):
    from haru_life_events.settings import health_outbox_url
    monkeypatch.setenv('HARU_VITALS_HEALTH_URL','http://localhost:9000/v1/health/vitals-outbox?limit=50')
    assert health_outbox_url().startswith('http://localhost:9000/')
    for bad in ('https://127.0.0.1:8768/v1/health/vitals-outbox','http://10.0.0.5:8768/v1/health/vitals-outbox',
                'http://user:pw@127.0.0.1:8768/v1/health/vitals-outbox','http://127.0.0.1:8768/v1/health/day'):
        monkeypatch.setenv('HARU_VITALS_HEALTH_URL',bad)
        with pytest.raises(RuntimeError):health_outbox_url()
