"""Edge detector: fake on_ground sequences, no network."""

from __future__ import annotations

import pytest

from airport_monitor.models import AircraftSample
from airport_monitor.state_machine import GroundStateTracker


def sample(
    on_ground: bool,
    ts: int,
    *,
    icao: str = "780abc",
    callsign: str | None = "CSZ9001",
) -> AircraftSample:
    return AircraftSample(icao24=icao, callsign=callsign, on_ground=on_ground, ts=ts)


def play(
    tracker: GroundStateTracker,
    flags: list[bool],
    *,
    start: int = 1_700_000_000,
    icao: str = "780abc",
    callsign: str | None = "CSZ9001",
) -> list[object]:
    events = []
    for offset, flag in enumerate(flags):
        events.append(
            tracker.observe(
                sample(flag, start + offset * 10, icao=icao, callsign=callsign),
            )
        )
    return events


def kinds(events: list[object]) -> list[str | None]:
    return [None if event is None else event.event for event in events]  # type: ignore[attr-defined]


def test_baseline_ground_and_air_emit_nothing() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    assert kinds(play(tracker, [True, True])) == [None, None]
    assert kinds(play(tracker, [False, False], icao="780abd")) == [None, None]


def test_true_to_false_is_takeoff() -> None:
    tracker = GroundStateTracker("zgsz", debounce_samples=2)
    events = play(tracker, [True, True, False, False])
    assert kinds(events) == [None, None, None, "takeoff"]
    event = events[-1]
    assert event is not None
    assert event.airport == "ZGSZ"  # type: ignore[attr-defined]
    assert event.icao24 == "780abc"  # type: ignore[attr-defined]
    assert event.callsign == "CSZ9001"  # type: ignore[attr-defined]
    assert event.ts == 1_700_000_030  # type: ignore[attr-defined]
    assert event.to_payload() == {  # type: ignore[attr-defined]
        "airport": "ZGSZ",
        "event": "takeoff",
        "icao24": "780abc",
        "callsign": "CSZ9001",
        "ts": 1_700_000_030,
    }


def test_false_to_true_is_landing() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    events = play(tracker, [False, False, True, True], callsign="CSZ3102")
    assert kinds(events) == [None, None, None, "landing"]
    assert events[-1].to_payload()["event"] == "landing"  # type: ignore[attr-defined]


def test_single_blip_does_not_emit_and_a_later_edge_still_does() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    events = play(tracker, [True, True, False, True, False, False])
    assert kinds(events) == [None, None, None, None, None, "takeoff"]


def test_flapping_never_commits() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    events = play(tracker, [False, False, True, False, True, False, True])
    assert kinds(events) == [None, None, None, None, None, None, None]


def test_debounce_of_one_fires_on_the_first_new_sample() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=1)
    events = play(tracker, [True, False])
    assert kinds(events) == [None, "takeoff"]


def test_full_cycle_takeoff_then_landing() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    events = play(tracker, [True, True, False, False, False, True, True])
    assert kinds(events) == [None, None, None, "takeoff", None, None, "landing"]


def test_tracks_are_independent() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    depart = play(tracker, [True, True, False, False], icao="aaaaaa", callsign="CSZ1")
    arrive = play(tracker, [False, False, True, True], icao="bbbbbb", callsign="CSZ2")
    assert kinds(depart)[-1] == "takeoff"
    assert kinds(arrive)[-1] == "landing"
    assert tracker.tracked_icao24s == frozenset({"aaaaaa", "bbbbbb"})


def test_callsign_is_stripped_and_kept_when_a_later_sample_omits_it() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    assert tracker.observe(sample(True, 10, callsign=" CSZ1 ")) is None
    assert tracker.observe(sample(True, 20, callsign=None)) is None
    assert tracker.observe(sample(False, 30, callsign=None)) is None
    event = tracker.observe(sample(False, 40, callsign=None))
    assert event is not None
    assert event.callsign == "CSZ1"


def test_callsign_can_arrive_on_the_confirming_sample() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    assert tracker.observe(sample(False, 10, callsign=None)) is None
    assert tracker.observe(sample(False, 20, callsign=None)) is None
    assert tracker.observe(sample(True, 30, callsign=None)) is None
    event = tracker.observe(sample(True, 40, callsign=" CSZ9 "))
    assert event is not None
    assert event.event == "landing"
    assert event.callsign == "CSZ9"


def test_same_opensky_timestamp_counts_as_one_sample() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    assert tracker.observe(sample(True, 100)) is None
    assert tracker.observe(sample(True, 100)) is None
    assert tracker.observe(sample(True, 110)) is None
    assert tracker.observe(sample(False, 120)) is None
    assert tracker.observe(sample(False, 120)) is None
    event = tracker.observe(sample(False, 130))
    assert event is not None and event.event == "takeoff"


def test_older_timestamp_does_not_disturb_a_pending_edge() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    assert tracker.observe(sample(True, 100)) is None
    assert tracker.observe(sample(True, 110)) is None
    assert tracker.observe(sample(False, 120)) is None
    # An older on-ground sample must not clear the pending takeoff.
    assert tracker.observe(sample(True, 100)) is None
    event = tracker.observe(sample(False, 130))
    assert event is not None and event.event == "takeoff"


def test_icao24_case_is_one_track() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2)
    tracker.observe(sample(True, 1, icao="780ABC"))
    tracker.observe(sample(True, 2, icao="780abc"))
    assert tracker.tracked_icao24s == frozenset({"780abc"})


def test_expire_drops_silent_tracks_and_the_next_session_rebaselines() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2, ttl_seconds=10)
    tracker.observe(sample(True, ts=100), seen_at=100)
    tracker.observe(sample(True, ts=110), seen_at=110)
    assert tracker.expire(120) == 0
    assert tracker.expire(120.1) == 1
    assert tracker.tracked_icao24s == frozenset()
    events = play(tracker, [False, False], start=200)
    assert kinds(events) == [None, None]


def test_stale_duplicate_still_refreshes_ttl() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2, ttl_seconds=10)
    tracker.observe(sample(True, ts=100), seen_at=100)
    tracker.observe(sample(True, ts=100), seen_at=109)
    assert tracker.expire(119) == 0
    assert tracker.expire(119.01) == 1


def test_debounce_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        GroundStateTracker("ZGSZ", debounce_samples=0)
