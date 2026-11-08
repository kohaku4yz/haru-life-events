"""Real Telegram-path envelopes use the same normalized timestamp as canaries."""
from datetime import datetime,timezone
from haru_life_events.life_events import LifeEventGate,Candidate
from haru_life_events.event_delivery_v2 import validate_event

def test_real_input_timestamp_same_string_as_observed_at(tmp_path):
    when=datetime(2026,10,12,7,14,13,tzinfo=timezone.utc)
    item=Candidate("life.location.changed","tg_unique_12345678",when,
        {"place":"東京都","observed_at":"2026-10-12T07:14:13Z",
         "reason":"sharing_started"})
    result=LifeEventGate(tmp_path/"gate.sqlite").consider(item,now=when,enqueue=True)
    assert result.emit
    event=validate_event(result.payload)
    assert event["timestamp"]=="2026-10-12T07:14:13Z"
    assert event["timestamp"]==event["data"]["observed_at"]
