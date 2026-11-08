"""Owner sharing -> timestamped reverse-geocoded change, strictly >=2 km."""
from __future__ import annotations
import tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from haru_life_events.reverse_geocoder import ResolvedPlace
from haru_life_events.telegram_location import HaruTelegramLocationAdapter,distance_m

NOW=datetime(2026,10,12,5,0,tzinfo=timezone.utc)
OWNER=1234
def sample(uid:int,lat:float=35.0,lon:float=139.0, *,mid:int=7,edit=False,
           seconds:int=0,live=3600,sender=OWNER,chat=OWNER):
    loc={"latitude":lat,"longitude":lon}
    if live is not None:loc["live_period"]=live
    msg={"message_id":mid,"date":int(NOW.timestamp()),"chat":{"id":chat},
         "from":{"id":sender},"location":loc}
    if edit:msg["edit_date"]=int(NOW.timestamp())+seconds
    else:msg["date"]+=seconds
    return {"update_id":uid,"edited_message" if edit else "message":msg}

class LocationTests(unittest.TestCase):
    def receiver(self,folder,resolve=None):
        return HaruTelegramLocationAdapter(allowed_user_id=OWNER,
            allowed_chat_id=OWNER,sqlite_path=Path(folder)/"loc.sqlite",
            place_resolver=resolve or (lambda lat,lon:ResolvedPlace("丸の内, 千代田区, 東京都","HeartRails Geo API")))

    def test_first_share_triggers_then_under_2km_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.receiver(d)
            first=a.observe(sample(1),now=NOW)
            self.assertEqual(len(first),1)
            self.assertEqual(first[0].data,{"place":"丸の内, 千代田区, 東京都",
                "observed_at":"2026-10-12T05:00:00Z","reason":"sharing_started"})
            self.assertNotIn("35.0",repr(first))
            self.assertNotIn("139.0",repr(first))
            self.assertEqual(a.observe(sample(2,35.009,139,edit=True,seconds=30),
                now=NOW+timedelta(seconds=30)),())
            changed=a.observe(sample(3,35.0181,139,edit=True,seconds=60),
                now=NOW+timedelta(seconds=60))
            self.assertEqual(len(changed),1)
            self.assertEqual(changed[0].data["reason"],"distance_2km")

    def test_last_triggered_anchor_not_last_polled(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.receiver(d)
            a.observe(sample(1),now=NOW)
            for idx,latitude in enumerate((35.005,35.01,35.015),start=2):
                self.assertEqual(a.observe(sample(idx,latitude,139,edit=True,seconds=idx*20),
                    now=NOW+timedelta(seconds=idx*20)),())
            self.assertEqual(len(a.observe(sample(5,35.0181,139,edit=True,seconds=100),
                now=NOW+timedelta(seconds=100))),1)

    def test_durable_state_replayed_edits_and_new_share(self):
        with tempfile.TemporaryDirectory() as d:
            first=self.receiver(d);first.observe(sample(1),now=NOW)
            restarted=self.receiver(d)
            self.assertEqual(restarted.observe(sample(1),now=NOW),())
            self.assertEqual(len(restarted.observe(sample(2,35.02,139,edit=True,seconds=60),
                now=NOW+timedelta(seconds=60))),1)
            self.assertEqual(restarted.observe(sample(3,35.02,139,edit=True,seconds=90,live=None),
                now=NOW+timedelta(seconds=90)),())
            self.assertEqual(restarted.observe(sample(4,35.08,139,edit=True,seconds=100),
                now=NOW+timedelta(seconds=100)),())
            new=restarted.observe(sample(5,35.08,139,mid=8,seconds=120),
                now=NOW+timedelta(seconds=120))
            self.assertEqual(new[0].data["reason"],"sharing_started")

    def test_private_chat_and_invalid_coordinates(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.receiver(d)
            for loc in (sample(1,sender=10),sample(2,chat=10),sample(3,lat=900),
                        sample(4,lat=True),sample(5,lon=float("nan"))):
                self.assertEqual(a.observe(loc,now=NOW),())
            self.assertFalse((Path(d)/'loc.sqlite').stat().st_mode & 0o077)

    def test_geocoder_failure_does_not_emit_or_advance_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            calls=0
            def resolver(lat,lon):
                nonlocal calls
                calls+=1
                return None if calls==1 else ResolvedPlace("新宿区, 東京都","GSI")
            a=self.receiver(d,resolver)
            self.assertEqual(a.observe(sample(1),now=NOW),())
            self.assertEqual(a.observe(sample(2,35,139,edit=True,seconds=15),
                                      now=NOW+timedelta(seconds=15)),())
            self.assertEqual(len(a.observe(sample(3,35,139,mid=9,seconds=30),
                now=NOW+timedelta(seconds=30))),1)

    def test_manual_static_location_is_explicit_one_shot_share(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.receiver(d)
            first=a.observe(sample(1,live=None),now=NOW)
            self.assertEqual(first[0].data["reason"],"sharing_started")
            self.assertEqual(a.observe(sample(2,35.04,139,edit=True,seconds=15,live=None),
                now=NOW+timedelta(seconds=15)),())
            second=a.observe(sample(3,35.04,139,mid=99,live=None,seconds=25),
                now=NOW+timedelta(seconds=25))
            self.assertEqual(second[0].data["reason"],"sharing_started")

    def test_threshold_precision(self):
        self.assertAlmostEqual(distance_m(35,139,35,139),0)
        self.assertLess(distance_m(35,139,35.017,139),2000)
        self.assertGreater(distance_m(35,139,35.019,139),2000)

if __name__=="__main__":unittest.main()
