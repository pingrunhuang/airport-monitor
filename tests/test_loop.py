"""Poll loop with a scripted OpenSky client."""

from __future__ import annotations

from airport_monitor.airports import Airport
from airport_monitor.alert import AlertDispatcher
from airport_monitor.ingest import OpenSkyError, OpenSkyRateLimitError
from airport_monitor.live import LiveBoard
from airport_monitor.loop import run_monitor
from airport_monitor.models import AircraftSample, BoundingBox, Snapshot
from airport_monitor.state_machine import GroundStateTracker


class Scripted:
    def __init__(
        self, snapshots: list[Snapshot], failures: dict[int, Exception] | None = None
    ) -> None:
        self.snapshots = list(snapshots)
        self.failures = failures or {}
        self.calls = 0

    def fetch(self, box: BoundingBox) -> Snapshot:
        self.calls += 1
        failure = self.failures.get(self.calls)
        if failure is not None:
            raise failure
        return self.snapshots.pop(0)


class Collect:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


def snap(on_ground: bool, ts: int, icao: str = "780abc") -> Snapshot:
    return Snapshot(
        time=ts,
        aircraft=(AircraftSample(icao24=icao, callsign="CSZ9001", on_ground=on_ground, ts=ts),),
        credits_remaining=100,
    )


def test_four_polls_emit_one_takeoff_and_do_not_sleep_after_the_last() -> None:
    client = Scripted(
        [
            snap(True, 100),
            snap(True, 110),
            snap(False, 120),
            snap(False, 130),
        ]
    )
    sink = Collect()
    slept: list[float] = []
    cycles = run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": GroundStateTracker("ZGSZ", debounce_samples=2)},
        dispatcher=AlertDispatcher([sink]),  # type: ignore[list-item]
        poll_interval_seconds=15,
        sleep=slept.append,
        clock=lambda: 1_000.0,
        max_cycles=4,
    )
    assert cycles == 4
    assert client.calls == 4
    assert slept == [15, 15, 15]
    assert len(sink.events) == 1
    assert sink.events[0].event == "takeoff"  # type: ignore[attr-defined]


def test_rate_limit_sleeps_for_retry_after_between_cycles() -> None:
    client = Scripted(
        [snap(True, 100)],
        failures={1: OpenSkyRateLimitError("limited", retry_after=5)},
    )
    slept: list[float] = []
    cycles = run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": GroundStateTracker("ZGSZ", debounce_samples=2)},
        dispatcher=AlertDispatcher([]),
        poll_interval_seconds=15,
        sleep=slept.append,
        max_cycles=2,
    )
    assert cycles == 2
    assert slept == [5]
    assert client.calls == 2


def test_request_error_keeps_the_loop_on_the_normal_interval() -> None:
    client = Scripted(
        [Snapshot(time=100, aircraft=())],
        failures={1: OpenSkyError("boom", status_code=500)},
    )
    slept: list[float] = []
    cycles = run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": GroundStateTracker("ZGSZ", debounce_samples=2)},
        dispatcher=AlertDispatcher([]),
        poll_interval_seconds=15,
        sleep=slept.append,
        max_cycles=2,
    )
    assert cycles == 2
    assert slept == [15]


def test_each_selected_airport_is_polled_once_per_cycle() -> None:
    # The shipped catalog is still ZGSZ only. The loop itself accepts a tuple
    # so phase 2 can pass more than one field without a second poll path.
    other = Airport(
        icao="YYYY",
        name="Synthetic",
        city="Nowhere",
        box=BoundingBox(lamin=1, lamax=2, lomin=3, lomax=4),
    )
    client = Scripted([Snapshot(time=1, aircraft=()), Snapshot(time=2, aircraft=())])
    cycles = run_monitor(
        airports=(_airport(), other),
        client=client,
        trackers={
            "ZGSZ": GroundStateTracker("ZGSZ"),
            "YYYY": GroundStateTracker("YYYY"),
        },
        dispatcher=AlertDispatcher([]),
        poll_interval_seconds=15,
        sleep=lambda seconds: None,
        max_cycles=1,
    )
    assert cycles == 1
    assert client.calls == 2


def test_board_receives_tracks_and_events_after_alerts() -> None:
    client = Scripted(
        [
            snap(True, 100),
            snap(True, 110),
            snap(False, 120),
            snap(False, 130),
        ]
    )
    sink = Collect()
    board = LiveBoard()
    cycles = run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": GroundStateTracker("ZGSZ", debounce_samples=2)},
        dispatcher=AlertDispatcher([sink]),  # type: ignore[list-item]
        poll_interval_seconds=15,
        sleep=lambda _seconds: None,
        clock=lambda: 1_000.0,
        max_cycles=4,
        board=board,
    )
    body = board.status(now=1_000.0)
    assert cycles == 4
    assert len(sink.events) == 1
    assert sink.events[0].event == "takeoff"  # type: ignore[attr-defined]
    assert body["poll_time"] == 130
    assert body["credits_remaining"] == 100
    assert body["name"] == "Shenzhen Bao'an International Airport"
    assert body["counts"]["events_since_start"] == 1  # type: ignore[index]
    assert body["events"][0]["event"] == "takeoff"  # type: ignore[index]
    assert body["tracks"][0]["state"] == "airborne"  # type: ignore[index]
    assert body["tracks"][0]["callsign"] == "CSZ9001"  # type: ignore[index]


def test_a_later_failure_keeps_tracks_already_on_the_board() -> None:
    board = LiveBoard()
    client = Scripted(
        [snap(True, 100)],
        failures={2: OpenSkyError("boom", status_code=500)},
    )
    run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": GroundStateTracker("ZGSZ", debounce_samples=1)},
        dispatcher=AlertDispatcher([]),
        poll_interval_seconds=15,
        sleep=lambda _seconds: None,
        clock=lambda: 50.0,
        max_cycles=2,
        board=board,
    )
    body = board.status(now=50.0)
    assert body["last_error"] == "boom"
    assert body["poll_time"] == 100
    assert body["tracks"][0]["icao24"] == "780abc"  # type: ignore[index]
    assert body["tracks"][0]["state"] == "on_ground"  # type: ignore[index]


def test_rate_limit_is_visible_on_the_board() -> None:
    board = LiveBoard()
    board.register("ZGSZ", name="Shenzhen Bao'an International Airport")
    client = Scripted([], failures={1: OpenSkyRateLimitError("limited", retry_after=5)})
    run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": GroundStateTracker("ZGSZ")},
        dispatcher=AlertDispatcher([]),
        poll_interval_seconds=15,
        max_cycles=1,
        board=board,
    )
    body = board.status()
    assert body["last_error"] == "OpenSky rate limit"
    assert body["tracks"] == []
    assert body["name"] == "Shenzhen Bao'an International Airport"


def test_expire_removes_aircraft_missing_from_this_poll() -> None:
    tracker = GroundStateTracker("ZGSZ", debounce_samples=2, ttl_seconds=10)
    tracker.observe(
        AircraftSample(icao24="aaaaaa", callsign=None, on_ground=True, ts=1),
        seen_at=0,
    )
    client = Scripted([Snapshot(time=50, aircraft=())])
    run_monitor(
        airports=(_airport(),),
        client=client,
        trackers={"ZGSZ": tracker},
        dispatcher=AlertDispatcher([]),
        poll_interval_seconds=15,
        clock=lambda: 50.0,
        max_cycles=1,
    )
    assert tracker.tracked_icao24s == frozenset()


def _airport() -> Airport:
    return Airport(
        icao="ZGSZ",
        name="Shenzhen Bao'an International Airport",
        city="Shenzhen",
        box=BoundingBox(lamin=22.58, lamax=22.70, lomin=113.74, lomax=113.88),
    )
