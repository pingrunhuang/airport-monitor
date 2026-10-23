"""Board store: copies of tracks, positions, and the event ring. No network."""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from airport_monitor.live import LiveBoard
from airport_monitor.models import AircraftSample, AlertEvent, Snapshot
from airport_monitor.state_machine import GroundStateTracker


def _sample(ts: int, *, on_ground: bool = False) -> AircraftSample:
    return AircraftSample(
        icao24="780ABC",
        callsign=" CSZ9001 ",
        on_ground=on_ground,
        ts=ts,
        latitude=22.6412,
        longitude=113.8101,
        baro_altitude=450.0,
        velocity=78.5,
        vertical_rate=6.0,
    )


def _event(ts: int, icao: str, kind: str = "takeoff") -> AlertEvent:
    return AlertEvent(
        airport="ZGSZ",
        event=kind,  # type: ignore[arg-type]
        icao24=icao,
        callsign="CSZ1",
        ts=ts,
    )


def test_positions_merge_onto_tracks_and_survive_a_quiet_poll() -> None:
    board = LiveBoard()
    tracker = GroundStateTracker("ZGSZ", debounce_samples=1)
    sample = _sample(100)
    tracker.observe(sample, seen_at=1_000.0)
    board.record_poll(
        "zgsz",
        Snapshot(time=100, aircraft=(sample,), credits_remaining=42),
        tracker,
        now=1_005.0,
        name="Shenzhen Bao'an International Airport",
    )
    body = board.status(now=1_008.0)
    assert body["airport"] == "ZGSZ"
    assert body["name"] == "Shenzhen Bao'an International Airport"
    assert body["poll_time"] == 100
    assert body["credits_remaining"] == 42
    assert body["aircraft_count"] == 1
    counts = body["counts"]
    assert counts["airborne"] == 1  # type: ignore[index]
    assert counts["on_ground"] == 0  # type: ignore[index]
    track = body["tracks"][0]  # type: ignore[index]
    assert track["icao24"] == "780abc"
    assert track["callsign"] == "CSZ9001"
    assert track["latitude"] == 22.6412
    assert track["longitude"] == 113.8101
    assert track["baro_altitude"] == 450.0
    assert track["velocity"] == 78.5
    assert track["vertical_rate"] == 6.0
    assert track["last_seen_age"] == 8.0
    track["callsign"] = "HACKED"
    board.record_poll(
        "ZGSZ",
        Snapshot(time=110, aircraft=(), credits_remaining=41),
        tracker,
        now=1_010.0,
    )
    kept = board.status(now=1_010.0)
    assert kept["aircraft_count"] == 0
    assert kept["poll_time"] == 110
    assert kept["last_error"] is None
    assert kept["tracks"][0]["callsign"] == "CSZ9001"  # type: ignore[index]
    assert kept["tracks"][0]["latitude"] == 22.6412  # type: ignore[index]


def test_event_ring_is_newest_first_and_today_follows_the_local_date() -> None:
    board = LiveBoard(event_limit=2)
    day = datetime(2026, 10, 23, 12, 0, 0)
    nxt = datetime(2026, 10, 24, 0, 30, 0)
    start = int(day.timestamp())
    board.record_events(
        "ZGSZ",
        [_event(start, "aaaaaa"), _event(start + 10, "bbbbbb"), _event(start + 20, "cccccc")],
    )
    body = board.status(now=day.timestamp() + 30)
    assert [row["icao24"] for row in body["events"]] == ["cccccc", "bbbbbb"]  # type: ignore[index, union-attr]
    assert body["counts"]["events_since_start"] == 3  # type: ignore[index]
    assert body["counts"]["events_today"] == 3  # type: ignore[index]
    later = board.status(now=nxt.timestamp())
    assert later["counts"]["events_today"] == 0  # type: ignore[index]
    assert later["counts"]["events_since_start"] == 3  # type: ignore[index]
    board.record_events("ZGSZ", [_event(int(nxt.timestamp()), "dddddd", "landing")])
    rolled = board.status(now=nxt.timestamp())
    assert rolled["counts"]["events_today"] == 1  # type: ignore[index]
    assert rolled["events"][0]["event"] == "landing"  # type: ignore[index]
    assert rolled["events"][0]["icao24"] == "dddddd"  # type: ignore[index]


def test_failure_keeps_tracks_and_a_later_poll_clears_it() -> None:
    board = LiveBoard()
    tracker = GroundStateTracker("ZGSZ", debounce_samples=1)
    sample = _sample(10, on_ground=True)
    tracker.observe(sample, seen_at=10.0)
    board.record_poll("ZGSZ", Snapshot(time=10, aircraft=(sample,)), tracker, now=10.0)
    board.record_failure("ZGSZ", "  boom\nbad  ")
    failed = board.status(now=12.0)
    assert failed["last_error"] == "boom bad"
    assert failed["tracks"][0]["state"] == "on_ground"  # type: ignore[index]
    board.record_poll("ZGSZ", Snapshot(time=20, aircraft=(sample,)), tracker, now=20.0)
    assert board.status(now=20.0)["last_error"] is None


def test_register_names_the_airport_before_the_first_poll() -> None:
    board = LiveBoard(started_at=10.0)
    empty = board.status(now=11.0)
    assert empty["airport"] is None
    assert empty["started_at"] == 10.0
    assert empty["generated_at"] == 11.0
    assert empty["counts"]["tracked"] == 0  # type: ignore[index]
    board.register("zgsz", name="Shenzhen Bao'an International Airport")
    named = board.status()
    assert named["airport"] == "ZGSZ"
    assert named["name"] == "Shenzhen Bao'an International Airport"
    other = board.status("yyyy")
    assert other["airport"] == "YYYY"
    assert other["tracks"] == []
    assert board.status()["airport"] == "ZGSZ"


def test_long_errors_are_trimmed_and_bad_limits_are_rejected() -> None:
    board = LiveBoard()
    board.record_failure("ZGSZ", "x" * 400)
    message = board.status()["last_error"]
    assert isinstance(message, str)
    assert len(message) == 300
    assert message.endswith("...")
    with pytest.raises(ValueError):
        LiveBoard(event_limit=0)
    with pytest.raises(ValueError):
        board.record_poll("ZGSZ", Snapshot(time=1), GroundStateTracker("YYYY"), now=1.0)
    with pytest.raises(ValueError):
        board.register("  ")


def test_status_can_run_beside_updates() -> None:
    board = LiveBoard()
    tracker = GroundStateTracker("ZGSZ", debounce_samples=1)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for index in range(40):
                sample = AircraftSample(
                    icao24="780abc",
                    callsign="CSZ1",
                    on_ground=True,
                    ts=index + 1,
                    latitude=22.6,
                    longitude=113.8,
                )
                tracker.observe(sample, seen_at=float(index))
                board.record_poll(
                    "ZGSZ",
                    Snapshot(time=index + 1, aircraft=(sample,), credits_remaining=3),
                    tracker,
                    now=float(index),
                )
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(40):
                body = board.status()
                tracks = body["tracks"]
                assert isinstance(tracks, list)
                tracks.append({"icao24": "nope"})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert board.status()["counts"]["tracked"] == 1  # type: ignore[index]
