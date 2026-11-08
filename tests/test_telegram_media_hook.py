"""Input-only Haru media hook with geocoded Work-ready envelopes."""
import tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from haru_life_events.reverse_geocoder import ResolvedPlace
from haru_life_events.telegram_media_hook import HaruMediaLocationHook,build_hook_from_config

NOW=datetime(2026,10,12,5,0,tzinfo=timezone.utc)
OWNER=123
def update(i,lat=35,lon=139,*,edit=False):
 m={"message_id":21,"date":int(NOW.timestamp()),"from":{"id":OWNER},
    "chat":{"id":OWNER},"location":{"latitude":lat,"longitude":lon,"live_period":3600}}
 if edit:m["edit_date"]=int(NOW.timestamp())+60
 return {"update_id":i,"edited_message" if edit else "message":m}

class HookTests(unittest.TestCase):
    def test_default_off_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(build_hook_from_config(owner_id=OWNER,root=d,env={}))
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_new_location_queued_once_with_timestamp_and_place(self):
        with tempfile.TemporaryDirectory() as d:
            hook=HaruMediaLocationHook(owner_id=OWNER,chat_id=OWNER,
                sqlite_path=Path(d)/"state.sqlite",
                place_resolver=lambda lat,lon:ResolvedPlace("新宿区, 東京都","HeartRails"))
            self.assertEqual(hook.observe_update(update(1),now=NOW),1)
            self.assertEqual(hook.observe_update(update(2,35.03,139,edit=True),
                         now=NOW+timedelta(seconds=60)),1)
            rows=hook.gate.pending()
            self.assertEqual([x["name"] for x in rows],
                             ["life.location.changed","life.location.changed"])
            self.assertEqual([x["data"]["reason"] for x in rows],
                             ["sharing_started","distance_2km"])
            for row in rows:
                self.assertEqual(row["data"]["place"],"新宿区, 東京都")
                self.assertIn("observed_at",row["data"])
                for sensitive in ("latitude","longitude","35.03","139.0"):
                    self.assertNotIn(sensitive,str(row))
            self.assertEqual(hook.observe_update(update(2,35.03,139,edit=True),
                        now=NOW+timedelta(seconds=60)),0)

    def test_config_needs_owner_private_chat_not_zones(self):
        with tempfile.TemporaryDirectory() as d:
            conf={"HARU_LOCATION_EVENTS_ENABLED":"1","HARU_LOCATION_CHAT_ID":str(OWNER)}
            hook=build_hook_from_config(owner_id=OWNER,root=d,env=conf)
            self.assertIsNotNone(hook)
            self.assertEqual(hook.observe_update({"update_id":1,"message":{"photo":[]}},now=NOW),0)
            with self.assertRaises(ValueError):
                build_hook_from_config(owner_id=OWNER,root=d,
                                       env=dict(conf,HARU_LOCATION_CHAT_ID="999"))
if __name__=="__main__":unittest.main()
