"""Validated, bounded Vitals transition + *fresh* multi-signal context.

Event payloads can include summarized private health metrics ONLY through
owner-authorized Work subscription. No raw histories, location, UUIDs, device
IDs or health notes. A missing measurement must be omitted, not guessed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

TRIGGERS = (
    "movement_started", "movement_settled",
    "heart_rate_rose", "heart_rate_fell",
    "sleep_started", "woke_up",
)
NUMERICAL_METRICS: dict[str, tuple[float, float, int]] = {
    # Metric -> min, max, maximum freshness in minutes.
    "heart_rate_bpm": (30, 240, 45),
    "steps_last_hour": (0, 20000, 90),
    "hrv_ms": (0, 1000, 1440),
    "active_energy_kcal_today": (0, 10000, 1440),
    "resting_heart_rate_bpm": (20, 220, 1440),
}
SLEEP_STATES = ("awake", "asleep", "unknown")
SLEEP_MAX_AGE_MIN = 720
METRIC_KEYS = (*NUMERICAL_METRICS.keys(), "sleep_state")
ALL_KEYS = frozenset(("triggers", "observed_at", "snapshot"))


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("expected timezone aware")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Reading:
    value: float | str
    observed_at: datetime
    source: str = "pulshealth"


def valid_vitals_data(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != ALL_KEYS:
        return False
    triggers = value.get("triggers")
    if not (isinstance(triggers,list) and 1 <= len(triggers) <= len(TRIGGERS)
            and all(isinstance(t,str) and t in TRIGGERS for t in triggers)
            and len(set(triggers)) == len(triggers)):
        return False
    if _time(value.get("observed_at")) is None:
        return False
    snap = value.get("snapshot")
    if not isinstance(snap, dict) or not set(snap).issubset(METRIC_KEYS):
        return False
    for field, item in snap.items():
        if not isinstance(item,dict) or set(item)!= {"value","observed_at"}:
            return False
        if _time(item.get("observed_at")) is None:
            return False
        v = item.get("value")
        if field == "sleep_state":
            if v not in SLEEP_STATES:
                return False
        else:
            lo, hi, _ = NUMERICAL_METRICS[field]
            if (isinstance(v,bool) or not isinstance(v,(float,int)) or
                not math.isfinite(v) or not lo <= v <= hi):
                return False
    return True


def build_vitals_data(
    triggers: str | list[str], observed_at: datetime,
    readings: Mapping[str, Reading], *,
    as_of: datetime,
) -> dict[str, Any]:
    """Assemble snapshot for ANY trigger, omitting stale/invalid data.

    No silent fallback to old HAE records. No staleness hiding: each
    included metric keeps its own actual measurement time. Use as_of
    (dispatch time), not the event time, for freshness checking.
    """
    names = [triggers] if isinstance(triggers,str) else triggers
    if not (isinstance(names,list) and names and len(names)<=len(TRIGGERS)
            and all(isinstance(x,str) and x in TRIGGERS for x in names)
            and len(set(names))==len(names)):
        raise ValueError("invalid Vitals triggers")
    event_time = _iso(observed_at)
    current = _iso(as_of)
    now = _time(current)
    assert now is not None
    if _time(event_time) is None:
        raise ValueError("invalid event time")
    snapshot: dict[str, dict[str, Any]] = {}
    for name, reading in readings.items():
        if name not in METRIC_KEYS:
            continue
        if not isinstance(reading,Reading) or reading.source != "pulshealth":
            continue
        try:
            time_str=_iso(reading.observed_at)
        except ValueError:
            continue
        recorded = _time(time_str)
        assert recorded is not None
        age = (now - recorded).total_seconds()/60
        max_age = (SLEEP_MAX_AGE_MIN if name == "sleep_state"
                   else NUMERICAL_METRICS[name][2])
        if not -1 <= age <= max_age:
            continue
        raw = reading.value
        if name == "sleep_state":
            if not isinstance(raw,str) or raw not in SLEEP_STATES:
                continue
            clean: float | str = raw
        else:
            lo,hi,_ = NUMERICAL_METRICS[name]
            if (isinstance(raw,bool) or not isinstance(raw,(float,int))
                or not math.isfinite(raw) or not lo <= raw <= hi):
                continue
            clean = int(round(raw)) if name in ("heart_rate_bpm", "steps_last_hour", "resting_heart_rate_bpm", "active_energy_kcal_today") else round(float(raw),1)
        snapshot[name]={"value":clean,"observed_at":time_str}
    data={"triggers":names,"observed_at":event_time,"snapshot":snapshot}
    if not valid_vitals_data(data):
        raise ValueError("invalid assembled Vitals data")
    return data


def _reading_schema_numeric(lo:float,hi:float) -> dict[str,Any]:
    return {
        "type":"object", "properties":{
            "value":{"type":"number","minimum":lo,"maximum":hi},
            "observed_at":{"type":"string","format":"date-time"}
        }, "required":["value","observed_at"],"additionalProperties":False,
    }


VITALS_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "triggers":{"type":"array","items":{"type":"string","enum":list(TRIGGERS)},
                    "minItems":1,"maxItems":len(TRIGGERS),"uniqueItems":True},
        "observed_at":{"type":"string","format":"date-time"},
        "snapshot":{
            "type":"object",
            "properties":{
                **{k:_reading_schema_numeric(lo,hi)
                   for k,(lo,hi,_) in NUMERICAL_METRICS.items()},
                "sleep_state":{
                    "type":"object",
                    "properties":{
                        "value":{"type":"string","enum":list(SLEEP_STATES)},
                        "observed_at":{"type":"string","format":"date-time"},
                    },
                    "required":["value","observed_at"],
                    "additionalProperties":False,
                },
            },
            "additionalProperties":False,
        },
    },
    "required":["triggers","observed_at","snapshot"],
    "additionalProperties":False,
}
