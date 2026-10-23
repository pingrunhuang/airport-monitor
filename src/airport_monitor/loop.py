"""Poll cycle: ingest, then the state machine, then alerts.

One cycle asks OpenSky for each selected airport, feeds the samples to
that airport's tracker, and emits any committed edges. HTTP 429 sleeps
for the server's retry hint instead of the normal interval. Other request
errors are logged and the loop continues.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from loguru import logger

from airport_monitor.airports import Airport
from airport_monitor.alert import AlertDispatcher
from airport_monitor.ingest import OpenSkyError, OpenSkyRateLimitError
from airport_monitor.live import LiveBoard
from airport_monitor.models import AlertEvent, BoundingBox, Snapshot
from airport_monitor.state_machine import GroundStateTracker


class StateSource(Protocol):
    def fetch(self, box: BoundingBox) -> Snapshot: ...


def run_monitor(
    *,
    airports: Sequence[Airport],
    client: StateSource,
    trackers: Mapping[str, GroundStateTracker],
    dispatcher: AlertDispatcher,
    poll_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    max_cycles: int | None = None,
    board: LiveBoard | None = None,
) -> int:
    """Poll until interrupted. Return how many cycles ran.

    ``max_cycles`` stops after N cycles and does not sleep after the last
    one, which is what ``--once`` uses. A rate-limit sleep still happens
    between cycles when more cycles remain.

    ``board``, when set, receives each successful poll and any events after
    the alert sinks run. A failed poll records an error and leaves the
    previous tracks in place. ``None`` keeps the alert path unchanged.
    """

    if max_cycles is not None and max_cycles < 1:
        raise ValueError("max_cycles must be >= 1")

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        backoff = _poll_once(
            airports=airports,
            client=client,
            trackers=trackers,
            dispatcher=dispatcher,
            poll_interval_seconds=poll_interval_seconds,
            clock=clock,
            board=board,
        )
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        sleep(poll_interval_seconds if backoff is None else backoff)
    return cycles


def _poll_once(
    *,
    airports: Sequence[Airport],
    client: StateSource,
    trackers: Mapping[str, GroundStateTracker],
    dispatcher: AlertDispatcher,
    poll_interval_seconds: float,
    clock: Callable[[], float],
    board: LiveBoard | None = None,
) -> float | None:
    """Poll every airport. Return a backoff in seconds when rate limited."""

    for airport in airports:
        try:
            snapshot = client.fetch(airport.box)
        except OpenSkyRateLimitError as exc:
            wait = poll_interval_seconds if exc.retry_after is None else max(exc.retry_after, 1.0)
            logger.warning(
                "OpenSky rate limit for {} — sleeping {:.0f}s before the next poll",
                airport.icao,
                wait,
            )
            if board is not None:
                board.record_failure(airport.icao, "OpenSky rate limit")
            return wait
        except OpenSkyError as exc:
            logger.error("OpenSky request failed for {}: {}", airport.icao, exc)
            if board is not None:
                board.record_failure(airport.icao, str(exc))
            continue

        now = clock()
        tracker = trackers[airport.icao]
        events: list[AlertEvent] = []
        for sample in snapshot.aircraft:
            event = tracker.observe(sample, seen_at=now)
            if event is not None:
                events.append(event)
        expired = tracker.expire(now)
        credits = snapshot.credits_remaining if snapshot.credits_remaining is not None else "-"
        logger.info(
            "{} poll t={} aircraft={} events={} tracked={} expired={} credits_remaining={}",
            airport.icao,
            snapshot.time,
            len(snapshot.aircraft),
            len(events),
            len(tracker.tracked_icao24s),
            expired,
            credits,
        )
        for event in events:
            logger.info("event {}", event.to_payload())
            dispatcher.emit(event)
        if board is not None:
            board.record_poll(airport.icao, snapshot, tracker, now=now, name=airport.name)
            board.record_events(airport.icao, events)
    return None
