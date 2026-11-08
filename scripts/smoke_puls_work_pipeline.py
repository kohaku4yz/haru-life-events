"""Offline TWO-repo synthetic proof: Puls DB -> Vitals -> context event -> fake Work.

Needs pulse-relay >= 0.2 importable (pip install or PYTHONPATH). No real health data, URLs or calls.
"""
from __future__ import annotations
import base64
import json
import sqlite3
import tempfile
import uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography.fernet import Fernet
from pulse_relay.store import initialize_store
from pulse_relay.extensions.vitals.outbox import read_vitals_outbox
from haru_life_events.life_events import LifeEventGate
from haru_life_events.event_bridge_v2 import EventSubscriptions
from haru_life_events.event_delivery_v2 import EventDeliveryQueue
from haru_life_events.puls_vitals_source import stage_from_health
from haru_life_events.context_payload import CONTEXT_NAME
from scripts.context_work_delivery import transfer_context_pending

NOW=datetime(2026,10,12,13,tzinfo=timezone.utc)
def z(x):return x.isoformat().replace('+00:00','Z')

class Verifier:
 def verify(self,*args):pass

class Sender:
 def __init__(self):self.events=[]
 def deliver(self,*,event,**kwargs):self.events.append(event);return 202

def main():
 with tempfile.TemporaryDirectory(prefix='vitals-synthetic-only-') as d:
  root=Path(d)
  source=root/'health';initialize_store(source)
  state=source/'vitals';state.mkdir(mode=0o700)
  with sqlite3.connect(state/'state.sqlite3') as db:
   db.execute('CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
   db.execute('INSERT INTO metadata VALUES (?,?)',('schema','vitals-v1'))
   db.execute('CREATE TABLE vitals_candidates(event_id TEXT PRIMARY KEY,event_name TEXT,observed_at TEXT,trigger_names TEXT,recorded_at TEXT)')
   db.execute('INSERT INTO vitals_candidates VALUES (?,?,?,?,?)',(
      'vit_'+'e'*48,'life.vitals.changed',z(NOW-timedelta(minutes=7)),
      json.dumps(['heart_rate_rose','movement_started']),z(NOW-timedelta(minutes=6))))
  with sqlite3.connect(source/'health.sqlite3') as db:
   at=z(NOW-timedelta(minutes=4))
   db.execute('INSERT INTO metric_samples(metric,observed_at,local_date,source,units,kind,payload_json,received_at) VALUES (?,?,?,?,?,?,?,?)',
              ('heart_rate',at,'2026-10-12','Apple Watch','count/min','quantity','{"qty":94}',at))
   db.execute('INSERT INTO puls_sample_map(uuid,metric,observed_at,source) VALUES (?,?,?,?)',
              (str(uuid.uuid4()),'heart_rate',at,'Apple Watch'))
  record=read_vitals_outbox(source,now=NOW)
  assert record['source']=='pulshealth_only'
  assert record['snapshot']['heart_rate_bpm']['value']==94
  gate=LifeEventGate(root/'gate.sqlite3')
  assert stage_from_health(gate,now=NOW,source=record)['staged']==1
  assert stage_from_health(gate,now=NOW,source=record)['staged']==0
  owner='demo-owner'
  store=EventSubscriptions(root/'subscriptions.sqlite',encryption_key=Fernet.generate_key(),identity_key=b'x'*32,verifier=Verifier())
  delivery={'mode':'webhook','url':'https://synthetic.invalid/collect','secret':'whsec_'+base64.b64encode(b'z'*32).decode()}
  store.subscribe(owner,{'name':CONTEXT_NAME,'arguments':{},'delivery':delivery},NOW)
  queue=EventDeliveryQueue(root/'deliveries.sqlite',store)
  location=LifeEventGate(root/'location-gate.sqlite3')
  assert transfer_context_pending(location=location,vitals=gate,queue=queue,owner=owner,now=NOW)['vitals_moved']==1
  sender=Sender()
  assert queue.drain_one(owner=owner,now=NOW,sender=sender)=='sent'
  assert len(sender.events)==1
  event=sender.events[0]
  assert event['name']==CONTEXT_NAME and event['data']['kind']=='vitals'
  assert event['data']['vitals']['triggers']==['heart_rate_rose','movement_started']
  assert event['data']['vitals']['snapshot']['heart_rate_bpm']['value']==94
  print('SYNTHETIC_PULS_TO_FAKE_WORK=PASS')
  print('HEALTH_SOURCE=pulshealth_only')
  print('ONE_CONTEXT_EVENT=1')
  print('TRIGGERS=2')
  print('REAL_NETWORK_SENDS=0')

if __name__=='__main__':main()
