"""One location-change event schema; no coordinates and no fixed zone list."""
from __future__ import annotations

from datetime import datetime,timezone

LOCATION_NAME = "life.location.changed"
LOCATION_REASON = ("sharing_started","distance_2km")
LOCATION_KEYS = frozenset(("place","observed_at","reason"))

LOCATION_PAYLOAD_SCHEMA = {
    "type":"object",
    "properties":{
        "place":{"type":"string","minLength":1,"maxLength":120},
        "observed_at":{"type":"string","format":"date-time"},
        "reason":{"type":"string","enum":list(LOCATION_REASON)},
    },
    "required":["place","observed_at","reason"],
    "additionalProperties":False,
}


def location_data_valid(value: object) -> bool:
    if not isinstance(value,dict) or set(value)!=LOCATION_KEYS:
        return False
    place=value["place"]
    if not isinstance(place,str) or not (1<=len(place)<=120):
        return False
    if place!=place.strip() or any(ord(c)<32 or ord(c)==127 for c in place):
        return False
    if value["reason"] not in LOCATION_REASON:
        return False
    stamp=value["observed_at"]
    if not isinstance(stamp,str):
        return False
    try:
        at=datetime.fromisoformat(stamp.replace("Z","+00:00"))
        if at.tzinfo is None or at.utcoffset() is None:
            return False
        at.astimezone(timezone.utc)
    except (ValueError,OverflowError):
        return False
    return True
