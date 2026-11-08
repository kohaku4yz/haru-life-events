from datetime import datetime,timedelta,timezone
import pytest
from haru_life_events.vitals_event_payload import (
    Reading,build_vitals_data,valid_vitals_data,VITALS_PAYLOAD_SCHEMA
)

NOW=datetime(2026,10,12,8,0,tzinfo=timezone.utc)
def sample():
    return {
        "heart_rate_bpm":Reading(83,NOW-timedelta(minutes=4)),
        "steps_last_hour":Reading(260,NOW-timedelta(minutes=15)),
        "sleep_state":Reading("awake",NOW-timedelta(hours=2)),
        "hrv_ms":Reading(48.7,NOW-timedelta(hours=4)),
        "resting_heart_rate_bpm":Reading(62,NOW-timedelta(hours=6)),
        "active_energy_kcal_today":Reading(187.8,NOW-timedelta(minutes=15)),
    }

@pytest.mark.parametrize("trigger",(
  "movement_started","movement_settled","heart_rate_rose","heart_rate_fell",
  "sleep_started","woke_up"
))
def test_any_signal_can_trigger_same_full_context(trigger):
    data=build_vitals_data(trigger,NOW,sample(),as_of=NOW)
    assert data["triggers"]==[trigger]
    assert set(data["snapshot"])==set(sample())
    assert data["snapshot"]["heart_rate_bpm"]["value"]==83
    assert data["snapshot"]["active_energy_kcal_today"]["value"]==188
    assert valid_vitals_data(data)

def test_stale_or_hae_input_not_conveyed_as_fresh():
    old=sample()
    old["heart_rate_bpm"]=Reading(150,NOW-timedelta(hours=3))
    old["steps_last_hour"]=Reading(250,NOW,source="hae")
    old["hrv_ms"]=Reading(47,NOW-timedelta(days=3))
    data=build_vitals_data("woke_up",NOW,old,as_of=NOW)
    assert "heart_rate_bpm" not in data["snapshot"]
    assert "steps_last_hour" not in data["snapshot"]
    assert "hrv_ms" not in data["snapshot"]
    assert "sleep_state" in data["snapshot"]

def test_bad_input_and_sensitive_extra_fields_rejected():
    data=build_vitals_data("heart_rate_rose",NOW,sample(),as_of=NOW)
    data["snapshot"]["heart_rate_bpm"]["device_id"]="private"
    assert not valid_vitals_data(data)
    assert not valid_vitals_data({"triggers":["heart_rate_rose"],"observed_at":"2026-10-12T08:00:00Z","snapshot":{},"latitude":35.2})

def test_partial_snapshot_still_can_be_a_trigger():
    result=build_vitals_data("sleep_started",NOW,{},as_of=NOW)
    assert result["snapshot"]=={}
    assert valid_vitals_data(result)

def test_schema_restricts_all_fields():
    schema=VITALS_PAYLOAD_SCHEMA
    assert set(schema["required"])=={"triggers","observed_at","snapshot"}
    assert not schema["additionalProperties"]
    assert not schema["properties"]["snapshot"]["additionalProperties"]

def test_simultaneous_signals_combine_in_one_nudge():
    data=build_vitals_data(["heart_rate_rose","movement_started"],NOW,sample(),as_of=NOW)
    assert data["triggers"]==["heart_rate_rose","movement_started"]
    assert valid_vitals_data(data)
    data["triggers"].append("movement_started")
    assert not valid_vitals_data(data)
