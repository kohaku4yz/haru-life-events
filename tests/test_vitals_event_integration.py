"""Public events/list and local gate accept full cross-signal snapshots only."""
from datetime import datetime,timedelta,timezone
import pytest
from haru_life_events.event_bridge_v2 import event_catalog
from haru_life_events.event_delivery_v2 import validate_event
from haru_life_events.life_events import Candidate,LifeEventGate
from haru_life_events.vitals_event_payload import Reading,build_vitals_data
from haru_life_events.context_payload import CONTEXT_NAME,wrap_vitals

NOW=datetime(2026,10,12,12,0,tzinfo=timezone.utc)


def as_context(envelope):
    """Vitals reach Work only wrapped in the shared context event."""
    return {**envelope, "name": CONTEXT_NAME, "data": wrap_vitals(envelope["data"])}

def payload():
    return build_vitals_data(["woke_up","movement_started"], NOW,
        {"sleep_state":Reading("awake",NOW-timedelta(minutes=5)),
         "heart_rate_bpm":Reading(76,NOW-timedelta(minutes=3)),
         "steps_last_hour":Reading(122,NOW-timedelta(minutes=20))},as_of=NOW)

def test_catalog_offers_vitals_only_inside_the_context_event():
    names={x["name"] for x in event_catalog()}
    assert "life.vitals.changed" not in names
    item=next(x for x in event_catalog() if x["name"]==CONTEXT_NAME)
    assert item["inputSchema"]["properties"]=={}
    assert "vitals" in item["payloadSchema"]["properties"]

def test_one_event_multiple_signals_and_context_at_any_hour(tmp_path):
    d=payload()
    gate=LifeEventGate(tmp_path/"gate.db")
    first=gate.consider(Candidate("life.vitals.changed","vit_change_00001",NOW,d),
                        now=NOW,enqueue=True)
    assert first.emit and first.reason=="accepted"
    assert validate_event(as_context(first.payload))==as_context(first.payload)
    assert first.payload["data"]["triggers"]==["woke_up","movement_started"]
    assert gate.consider(Candidate("life.vitals.changed","vit_change_00001",NOW,d),
                         now=NOW).reason=="duplicate"
    # No long daily wellness-trend cooldown for fresh independent changes.
    other=gate.consider(Candidate("life.vitals.changed","vit_change_00002",
        NOW+timedelta(minutes=5),
        build_vitals_data("heart_rate_rose", NOW+timedelta(minutes=5),{},
                          as_of=NOW+timedelta(minutes=5))),
        now=NOW+timedelta(minutes=5))
    assert other.emit and other.reason=="accepted"

def test_event_rejects_raw_medical_history_and_personal_text():
    env={"eventId":"vit_change_00001","name":"life.vitals.changed",
         "timestamp":"2026-10-12T12:00:00Z","data":payload(),"cursor":None}
    assert validate_event(as_context(env))==as_context(env)
    env["data"]["snapshot"]["heart_rate_bpm"]["raw_samples"]=[70,72,80]
    with pytest.raises(ValueError):
        validate_event(as_context(env))
    with pytest.raises(ValueError):
        validate_event(env)  # never deliverable on its own
