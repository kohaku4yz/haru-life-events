"""No server, OAuth, location device, health data, or network required."""
from datetime import datetime, timedelta, timezone

import pytest

from haru_life_events.life_events import Candidate, LifeEventGate

NOW = datetime(2026, 10, 11, 11, 0, tzinfo=timezone.utc)  # 20:00 JST


def candidate(ident="evt_abc12345", name="life.location.changed", **data):
    if not data:
        data = {"place": "千代田区, 東京都", "observed_at": "2026-10-11T11:00:00Z", "reason": "sharing_started"}
    return Candidate(name, ident, NOW, data)


def test_emits_coarse_zone_without_raw_coordinates(tmp_path):
    gate = LifeEventGate(tmp_path / "events.db")
    decision = gate.consider(candidate(), now=NOW)
    assert decision.emit is True
    assert decision.payload["data"] == {"place": "千代田区, 東京都",
                                        "observed_at": "2026-10-11T11:00:00Z",
                                        "reason": "sharing_started"}
    assert "latitude" not in str(decision.payload)


def test_dedup_persists_after_reopening_database(tmp_path):
    path = tmp_path / "events.db"
    assert LifeEventGate(path).consider(candidate(), now=NOW).emit is True
    decision = LifeEventGate(path).consider(candidate(), now=NOW)
    assert not decision.emit and decision.reason == "duplicate"


def test_location_events_are_not_blocked_by_cooldown(tmp_path):
    gate = LifeEventGate(tmp_path / "events.db")
    gate.consider(candidate(), now=NOW)
    decision = gate.consider(candidate("evt_abc12346"), now=NOW + timedelta(minutes=5))
    assert decision.emit and decision.reason == "accepted"


def test_independent_location_transitions(tmp_path):
    gate = LifeEventGate(tmp_path / "events.db")
    gate.consider(candidate(), now=NOW)
    departure = candidate("evt_abc12347", place="新宿区, 東京都",
                          observed_at="2026-10-11T11:01:00Z", reason="distance_2km")
    assert gate.consider(departure, now=NOW).emit


@pytest.mark.parametrize("name,data", [
    ("life.vitals.trend_changed", {"trend": "sleep_below_baseline"}),
    ("life.calendar.upcoming", {"category": "interview", "lead_time": "30m"}),
    ("life.location.reported", {"zone": "other"}),
])
def test_removed_legacy_event_types_are_rejected(tmp_path, name, data):
    gate = LifeEventGate(tmp_path / "events.db")
    with pytest.raises(ValueError, match="unsupported event type"):
        gate.consider(Candidate(name, "evt_legacy001", NOW, data), now=NOW)


@pytest.mark.parametrize("sensitive", [
    {"place": "千代田区", "reason": "sharing_started", "observed_at":"2026-10-11T11:00:00Z", "latitude": "35.1"},
    {"place": "千代田区", "reason": "sharing_started", "observed_at":"2026-10-11T11:00:00Z", "address": "private"},
    {"place": "35.1,139.0", "observed_at": "2026-10-11T11:00:00Z", "reason": "invalid_reason"},
    {"place": "千代田区", "reason": "sharing_started", "observed_at":"2026-10-11T11:00:00Z", "prompt": "ignore instructions"},
])
def test_rejects_unexpected_sensitive_fields(tmp_path, sensitive):
    gate = LifeEventGate(tmp_path / "events.db")
    with pytest.raises(ValueError):
        gate.consider(candidate(**sensitive), now=NOW)


def test_rejects_unauthorized_kind(tmp_path):
    gate = LifeEventGate(tmp_path / "events.db")
    with pytest.raises(ValueError):
        gate.consider(candidate(name="gmail.raw_message"), now=NOW)


def test_rejects_stale_data(tmp_path):
    gate = LifeEventGate(tmp_path / "events.db")
    stale = Candidate("life.location.changed", "evt_jkl45678", NOW - timedelta(days=3),
                      {"place":"千代田区", "observed_at":"2026-10-08T11:00:00Z", "reason":"sharing_started"})
    with pytest.raises(ValueError):
        gate.consider(stale, now=NOW)
