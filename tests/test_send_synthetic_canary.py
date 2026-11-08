from datetime import datetime, timezone
from haru_life_events.event_delivery_v2 import validate_event
from scripts.send_synthetic_canary import TEST_EVENT_ID, EVENT_NAME, build_envelope

def test_synthetic_location_envelope_contains_no_sensitive_fields():
    event = build_envelope(datetime(2026,10,12,5,10,tzinfo=timezone.utc))
    assert event == validate_event(event)
    assert event["eventId"] == TEST_EVENT_ID
    assert event["name"] == EVENT_NAME
    assert event["data"] == {"place":"模拟区域（非真实地名）","observed_at":"2026-10-12T05:10:00Z","reason":"sharing_started"}
    assert "synthetic" in event["eventId"]
    assert not any(k in repr(event).lower() for k in ("latitude","longitude","heart_rate","chat_id"))

def test_schema_v2_unique_delivery_id():
    from scripts.send_synthetic_canary import SCHEMA_V2_EVENT_ID
    sample=build_envelope(datetime(2026,10,12,6,15,tzinfo=timezone.utc),event_id=SCHEMA_V2_EVENT_ID)
    assert sample['eventId']==SCHEMA_V2_EVENT_ID
    assert set(sample['data'])=={'place','observed_at','reason'}
    assert sample['data']['observed_at']==sample['timestamp']
