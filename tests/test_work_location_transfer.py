"""Private geocoded-event outbox to Work queue; no network or Telegram token."""
from datetime import datetime,timezone
from cryptography.fernet import Fernet
from haru_life_events.life_events import LifeEventGate,Candidate
from haru_life_events.event_bridge_v2 import EventSubscriptions
from haru_life_events.event_delivery_v2 import EventDeliveryQueue
from scripts.telegram_work_delivery import transfer_pending

NOW=datetime(2026,10,12,6,0,tzinfo=timezone.utc)
class Echo:
 def verify(self,*args,**kwargs):pass

def test_outbox_replay_and_ack(tmp_path):
 gate=LifeEventGate(tmp_path/"source.db")
 store=EventSubscriptions(tmp_path/"subs.db",encryption_key=Fernet.generate_key(),
                          identity_key=b"i"*40,verifier=Echo())
 queue=EventDeliveryQueue(tmp_path/"delivery.db",store)
 payload=Candidate("life.location.changed","evt_location_01",NOW,
     {"place":"千代田区, 東京都","observed_at":"2026-10-12T06:00:00Z","reason":"sharing_started"})
 assert gate.consider(payload,now=NOW,enqueue=True).emit
 assert transfer_pending(gate,queue,"cat",NOW)==0
 assert len(gate.pending())==1 # No subscriber: leave pending
 import base64
 store.subscribe("cat",{"name":"life.location.changed","arguments":{},
  "delivery":{"mode":"webhook","url":"https://example.org/notify",
  "secret":"whsec_"+base64.b64encode(b"k"*32).decode()}},NOW)
 assert transfer_pending(gate,queue,"cat",NOW)==1
 assert len(gate.pending())==0
 assert transfer_pending(gate,queue,"cat",NOW)==0
 assert queue.counts("cat")=={"pending":1}
 assert queue.already_enqueued(owner="cat",event_id="evt_location_01")
 assert gate.consider(payload,now=NOW,enqueue=True).reason=="duplicate"
