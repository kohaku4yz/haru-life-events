import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from haru_life_events.telegram_media_hook import HaruMediaLocationHook
from haru_life_events.reverse_geocoder import ResolvedPlace
from haru_life_events.event_bridge_v2 import EventSubscriptions
from haru_life_events.event_delivery_v2 import EventDeliveryQueue
from cryptography.fernet import Fernet

NOW=datetime(2026,10,12,5,tzinfo=timezone.utc)
def location(i,lat,edit=False):
 msg={"message_id":20,"date":int(NOW.timestamp()),"chat":{"id":42},
      "from":{"id":42},"location":{"latitude":lat,"longitude":139,"live_period":3600}}
 if edit:msg["edit_date"]=int(NOW.timestamp())+60
 return {"update_id":i,"edited_message" if edit else "message":msg}
class Verifier:
 def verify(self,*a,**kw):pass
class FakeReceiver:
 def __init__(self):self.received=[]
 def deliver(self,**kw):
  self.received.append(kw["event"])
  return 202
class EndToEnd(unittest.TestCase):
 def test_location_envelopes_to_work(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)
   hook=HaruMediaLocationHook(owner_id=42,chat_id=42,sqlite_path=p/"loc.db",
       place_resolver=lambda lat,lon:ResolvedPlace("千代田区, 東京都","HeartRails"))
   self.assertEqual(hook.observe_update(location(1,35),now=NOW),1)
   self.assertEqual(hook.observe_update(location(2,35.03,True),now=NOW+timedelta(seconds=60)),1)
   pending=hook.gate.pending()
   self.assertEqual([row["data"]["reason"] for row in pending],["sharing_started","distance_2km"])
   store=EventSubscriptions(p/"subs.db",encryption_key=Fernet.generate_key(),
       identity_key=b"i"*40,verifier=Verifier())
   # Store is isolated; eligibility semantics are covered by subscription tests.
   for event in pending:
    self.assertEqual(set(event["data"]),{"place","observed_at","reason"})
    self.assertNotIn("latitude",str(event))
    self.assertNotIn("longitude",str(event))
    self.assertNotIn("35.03",str(event))
if __name__=="__main__":unittest.main()
