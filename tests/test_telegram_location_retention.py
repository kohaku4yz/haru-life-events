from datetime import datetime,timedelta,timezone
from pathlib import Path
import sqlite3
from haru_life_events.telegram_location import HaruTelegramLocationAdapter
from haru_life_events.reverse_geocoder import ResolvedPlace
from scripts.telegram_work_delivery import scrub_expired_anchors

T=datetime(2026,10,12,5,tzinfo=timezone.utc)
def update(live):
    fields={"latitude":35.12,"longitude":139.15}
    if live is not None: fields["live_period"]=live
    return {"update_id":1,"message":{"message_id":3,"date":int(T.timestamp()),
            "from":{"id":12},"chat":{"id":12},"location":fields}}

def test_static_share_does_not_persist_coordinates(tmp_path):
    p=tmp_path/"loc.db"
    a=HaruTelegramLocationAdapter(allowed_user_id=12,allowed_chat_id=12,
      sqlite_path=p,place_resolver=lambda a,b:ResolvedPlace("東京都","HeartRails"))
    assert len(a.observe(update(None),now=T))==1
    with sqlite3.connect(p) as db:
        assert db.execute("select anchor_lat,anchor_lon,active from location_shares").fetchone()==(0,0,0)

def test_expired_live_anchor_scrubbed(tmp_path):
    p=tmp_path/"loc.db"
    a=HaruTelegramLocationAdapter(allowed_user_id=12,allowed_chat_id=12,
      sqlite_path=p,place_resolver=lambda a,b:ResolvedPlace("東京都","HeartRails"))
    assert len(a.observe(update(900),now=T))==1
    assert scrub_expired_anchors(p,T+timedelta(hours=1))==1
    with sqlite3.connect(p) as db:
        assert db.execute("select anchor_lat,anchor_lon,active from location_shares").fetchone()==(0,0,0)
