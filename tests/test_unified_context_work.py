"""One Work event includes location OR Vitals, with safe handover."""
import base64
import json
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from cryptography.fernet import Fernet
from haru_life_events.context_payload import CONTEXT_NAME,valid_context_data,wrap_location,wrap_vitals
from haru_life_events.event_bridge_v2 import EventSubscriptions,event_catalog
from haru_life_events.event_delivery_v2 import EventDeliveryQueue,validate_event
from haru_life_events.life_events import Candidate,LifeEventGate
from haru_life_events.vitals_event_payload import build_vitals_data,Reading
from scripts.context_work_delivery import context_enabled,transfer_context_pending,MARKER
from scripts.telegram_work_delivery import transfer_pending

T=datetime(2026,10,12,14,tzinfo=timezone.utc)
class Verifier:
 def verify(self,*args):pass
class Sender:
 def __init__(self):self.events=[]
 def deliver(self,*,event,**kwargs):self.events.append(event);return 202

def store(tmp):
 s=EventSubscriptions(tmp/'subscriptions.sqlite',encryption_key=Fernet.generate_key(),identity_key=b'w'*32,verifier=Verifier())
 cb={'mode':'webhook','url':'https://events.example.com/callback','secret':'whsec_'+base64.b64encode(b't'*32).decode()}
 s.subscribe('demo-owner',{'name':'life.location.changed','delivery':cb},T)
 s.subscribe('demo-owner',{'name':CONTEXT_NAME,'delivery':cb},T)
 return s

def staged(tmp):
 loc=LifeEventGate(tmp/'location.sqlite3')
 vit=LifeEventGate(tmp/'vitals.sqlite3')
 location={'place':'神奈川県 横浜市','observed_at':T.isoformat(),'reason':'distance_2km'}
 h=Candidate('life.location.changed','tg_1234567890111234567890',T,location)
 assert loc.consider(h,now=T,enqueue=True).emit
 data=build_vitals_data(['woke_up','heart_rate_rose'],T,{'heart_rate_bpm':Reading(84,T)},as_of=T)
 v=Candidate('life.vitals.changed','vit_'+'f'*48,T,data)
 assert vit.consider(v,now=T,enqueue=True).emit
 return loc,vit

def test_one_catalog_schema_two_distinct_payload_types():
 cat={x['name']:x for x in event_catalog()}
 assert CONTEXT_NAME in cat
 assert cat[CONTEXT_NAME]['payloadSchema']['oneOf']
 loc=wrap_location({'place':'横浜','observed_at':T.isoformat(),'reason':'sharing_started'})
 vit=wrap_vitals(build_vitals_data('woke_up',T,{},as_of=T))
 assert valid_context_data(loc) and valid_context_data(vit)
 assert not valid_context_data({'kind':'location','location':loc['location'],'vitals':vit['vitals']})
 assert not valid_context_data({'kind':'vitals','vitals':{'triggers':['blood_pressure'],'observed_at':T.isoformat(),'snapshot':{}}})
 assert not valid_context_data({'kind':'vitals','vitals':vit['vitals'],'raw_gps':[0,0]})
 for data in (loc,vit):
  validate_event({'eventId':'evt_12345678','name':CONTEXT_NAME,'timestamp':T.isoformat(),'cursor':None,'data':data})

def test_single_subscriber_takes_both_without_location_duplicate(tmp_path):
 owner='demo-owner'
 loc,vit=staged(tmp_path)
 s=store(tmp_path);q=EventDeliveryQueue(tmp_path/'deliveries.sqlite',s)
 marker=tmp_path/MARKER
 assert context_enabled(tmp_path,s,owner,T) is False
 marker.write_text('CONTEXT_ROUTE_V1\n');marker.chmod(0o600)
 assert context_enabled(tmp_path,s,owner,T) is True
 moved=transfer_context_pending(location=loc,vitals=vit,queue=q,owner=owner,now=T)
 assert moved=={'location_moved':1,'vitals_moved':1,'vitals_expired':0,'location_expired':0}
 assert loc.pending()==[] and vit.pending()==[]
 assert transfer_context_pending(location=loc,vitals=vit,queue=q,owner=owner,now=T)['location_moved']==0
 send=Sender()
 assert q.drain_one(owner=owner,now=T,sender=send)=='sent'
 assert q.drain_one(owner=owner,now=T,sender=send)=='sent'
 assert q.drain_one(owner=owner,now=T,sender=send)=='empty'
 assert len(send.events)==2
 assert {e['name'] for e in send.events}=={CONTEXT_NAME}
 assert {e['data']['kind'] for e in send.events}=={'location','vitals'}
 assert len(s.eligible(owner,'life.location.changed',T))==1
 assert len(s.eligible(owner,CONTEXT_NAME,T))==1
 assert send.events[0]['eventId']!=send.events[1]['eventId']

def test_legacy_location_stays_unchanged_if_not_armed(tmp_path):
 loc,_=staged(tmp_path)
 s=store(tmp_path);q=EventDeliveryQueue(tmp_path/'deliveries.sqlite',s)
 assert context_enabled(tmp_path,s,'demo-owner',T) is False
 assert transfer_pending(loc,q,'demo-owner',T)==1
 send=Sender()
 assert q.drain_one(owner='demo-owner',now=T,sender=send)=='sent'
 assert send.events[0]['name']=='life.location.changed'

def test_marker_refuses_unsafe_or_no_single_sub(tmp_path):
 s=store(tmp_path)
 p=tmp_path/MARKER
 p.write_text('CONTEXT_ROUTE_V1');p.chmod(0o644)
 with pytest.raises(ValueError):context_enabled(tmp_path,s,'demo-owner',T)
 p.chmod(0o600)
 assert context_enabled(tmp_path,s,'demo-owner',T)
 p.unlink()
 p.symlink_to(tmp_path/'missing')
 with pytest.raises(ValueError):context_enabled(tmp_path,s,'demo-owner',T)

def test_old_vitals_expire_before_cutover(tmp_path):
 loc,vit=staged(tmp_path)
 s=store(tmp_path);q=EventDeliveryQueue(tmp_path/'deliveries.sqlite',s)
 later=T+timedelta(minutes=92)
 outcome=transfer_context_pending(location=loc,vitals=vit,queue=q,owner='demo-owner',now=later)
 assert outcome['vitals_expired']==1
 assert outcome['location_moved']==1
 assert vit.pending()==[]
 send=Sender(); assert q.drain_one(owner='demo-owner',now=later,sender=send)=='sent'
 assert len(send.events)==1 and send.events[0]['data']['kind']=='location'
