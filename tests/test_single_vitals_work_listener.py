"""One context subscription handles every Vitals reason; no standalone Vitals event.

All HTTPS is faked. The location-only subscription remains independent.
"""
import base64
import pytest
from datetime import datetime,timezone,timedelta
from cryptography.fernet import Fernet
from haru_life_events.event_bridge_v2 import EventSubscriptions
from haru_life_events.event_delivery_v2 import EventDeliveryQueue
from haru_life_events.vitals_event_payload import build_vitals_data,Reading
from haru_life_events.context_payload import CONTEXT_NAME,wrap_vitals

NOW=datetime(2026,10,12,9,tzinfo=timezone.utc)
class Verified:
    def verify(self,url,secret,sid,now):pass

class FakeSender:
    def __init__(self):self.events=[]
    def deliver(self,**args):
        self.events.append(args['event'])
        return 202

def test_one_context_listener_and_existing_location_untouched(tmp_path):
    owner='only-owner'
    store=EventSubscriptions(tmp_path/'subscriptions.sqlite',
        encryption_key=Fernet.generate_key(),identity_key=b'f'*32,verifier=Verified())
    delivery={'mode':'webhook','url':'https://events.example.org/dispatch',
              'secret':'whsec_'+base64.b64encode(b'v'*32).decode()}
    store.subscribe(owner,{'name':'life.location.changed','arguments':{},'delivery':delivery},NOW)
    with pytest.raises(ValueError):
        store.subscribe(owner,{'name':'life.vitals.changed','arguments':{},'delivery':delivery},NOW)
    store.subscribe(owner,{'name':CONTEXT_NAME,'arguments':{},'delivery':delivery},NOW)
    assert len(store.eligible(owner,CONTEXT_NAME,NOW))==1
    assert len(store.eligible(owner,'life.location.changed',NOW))==1
    queue=EventDeliveryQueue(tmp_path/'deliveries.sqlite',store)
    sender=FakeSender()
    for index,trigger in enumerate(('movement_started','heart_rate_rose','woke_up')):
        t=NOW+timedelta(minutes=index)
        data=build_vitals_data(trigger,t,{'heart_rate_bpm':Reading(81,t)},as_of=t)
        envelope={'eventId':f'vit_sensor_{index:04}', 'name':CONTEXT_NAME,
                  'timestamp':t.isoformat(),'data':wrap_vitals(data),'cursor':None}
        assert queue.enqueue(owner=owner,event=envelope,now=t)==1
        assert queue.drain_one(owner=owner,now=t,sender=sender)=='sent'
    assert len(sender.events)==3
    assert {x['name'] for x in sender.events}=={CONTEXT_NAME}
    assert [x['data']['vitals']['triggers'][0] for x in sender.events]==[
        'movement_started','heart_rate_rose','woke_up']
    assert len(store.eligible(owner,'life.location.changed',NOW))==1
