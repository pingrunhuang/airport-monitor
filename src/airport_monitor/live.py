"""In-process board the poll loop writes and the local page reads.

The detector keeps mutable ``_Track`` objects. This store copies the bits
the page needs (track rows, last position sample, recent alerts) under a
lock so the HTTP thread never touches the state machine.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from airport_monitor.models import AircraftSample, AlertEvent, Snapshot
from airport_monitor.state_machine import GroundStateTracker

_ERROR_LIMIT = 300
_DEFAULT_EVENT_LIMIT = 50


@dataclass
class _AirportView:
    name: str | None = None
    poll_time: int | None = None
    polled_at: float | None = None
    aircraft_count: int = 0
    credits_remaining: int | None = None
    tracks: list[dict[str, object]] = field(default_factory=list)
    samples: dict[str, dict[str, object]] = field(default_factory=dict)
    events: deque[dict[str, object]] = field(default_factory=deque)
    events_since_start: int = 0
    events_today: int = 0
    event_day: date | None = None
    last_error: str | None = None


class LiveBoard:
    """Latest poll, track rows, and a ring of recent alert payloads."""

    def __init__(
        self,
        *,
        event_limit: int = _DEFAULT_EVENT_LIMIT,
        started_at: float | None = None,
    ) -> None:
        if event_limit < 1:
            raise ValueError("event_limit must be >= 1")
        self.event_limit = event_limit
        self._started_at = time.time() if started_at is None else float(started_at)
        self._lock = threading.Lock()
        self._primary: str | None = None
        self._airports: dict[str, _AirportView] = {}

    def register(self, airport: str, *, name: str | None = None) -> None:
        """Remember an airport before the first poll so the page can name it."""

        code = _airport_code(airport)
        with self._lock:
            view = self._touch(code)
            if name:
                view.name = name

    def record_poll(
        self,
        airport: str,
        snapshot: Snapshot,
        tracker: GroundStateTracker,
        *,
        now: float,
        name: str | None = None,
    ) -> None:
        """Replace track rows from ``tracker.snapshot()`` and store positions.

        Samples are keyed by icao24. A track that was not in this response
        keeps its previous position until ``expire`` drops it. A successful
        poll clears ``last_error``.
        """

        code = _airport_code(airport)
        if tracker.airport != code:
            raise ValueError(f"tracker airport {tracker.airport} does not match {code}")
        rows = tracker.snapshot(now=now)
        fresh = {sample.icao24: _position(sample) for sample in snapshot.aircraft}
        with self._lock:
            view = self._touch(code)
            if name:
                view.name = name
            view.poll_time = snapshot.time
            view.polled_at = float(now)
            view.aircraft_count = len(snapshot.aircraft)
            view.credits_remaining = snapshot.credits_remaining
            view.tracks = rows
            view.last_error = None
            tracked = {str(row["icao24"]) for row in rows}
            merged: dict[str, dict[str, object]] = {}
            for icao24 in tracked:
                if icao24 in fresh:
                    merged[icao24] = fresh[icao24]
                elif icao24 in view.samples:
                    merged[icao24] = view.samples[icao24]
            view.samples = merged

    def record_events(self, airport: str, events: Sequence[AlertEvent]) -> None:
        """Append alert payloads. The newest is first; the ring keeps ``event_limit``."""

        if not events:
            return
        code = _airport_code(airport)
        with self._lock:
            view = self._touch(code)
            for event in events:
                view.events.appendleft(event.to_payload())
                view.events_since_start += 1
                day = datetime.fromtimestamp(event.ts).date()
                if view.event_day is None or day > view.event_day:
                    view.event_day = day
                    view.events_today = 0
                if day == view.event_day:
                    view.events_today += 1

    def record_failure(self, airport: str, message: str) -> None:
        """Note a failed poll without clearing tracks already on the board."""

        code = _airport_code(airport)
        with self._lock:
            view = self._touch(code)
            view.last_error = _clean_error(message)

    def status(self, airport: str | None = None, *, now: float | None = None) -> dict[str, object]:
        """JSON-ready view. Ages are measured at ``now`` (default: the wall clock)."""

        moment = time.time() if now is None else float(now)
        requested = airport.strip().upper() if airport and airport.strip() else None
        with self._lock:
            code = requested or self._primary
            if code is None:
                return self._blank(None, moment)
            view = self._airports.get(code)
            if view is None:
                return self._blank(code, moment)
            return self._payload(code, view, moment)

    def _touch(self, code: str) -> _AirportView:
        if self._primary is None:
            self._primary = code
        view = self._airports.get(code)
        if view is None:
            view = _AirportView(events=deque(maxlen=self.event_limit))
            self._airports[code] = view
        return view

    def _blank(self, code: str | None, now: float) -> dict[str, object]:
        return {
            "airport": code,
            "name": None,
            "poll_time": None,
            "polled_at": None,
            "credits_remaining": None,
            "aircraft_count": 0,
            "last_error": None,
            "started_at": self._started_at,
            "generated_at": now,
            "counts": _counts(0, 0, 0, 0, 0, 0),
            "tracks": [],
            "events": [],
        }

    def _payload(self, code: str, view: _AirportView, now: float) -> dict[str, object]:
        on_ground = 0
        airborne = 0
        pending = 0
        tracks: list[dict[str, object]] = []
        for row in view.tracks:
            icao24 = str(row["icao24"])
            sample = view.samples.get(icao24, {})
            state = str(row["state"])
            if state == "on_ground":
                on_ground += 1
            elif state == "airborne":
                airborne += 1
            else:
                pending += 1
            seen = row["last_seen"]
            last_seen = float(seen) if isinstance(seen, int | float) else 0.0
            tracks.append(
                {
                    "icao24": icao24,
                    "callsign": row["callsign"],
                    "state": state,
                    "last_seen": last_seen,
                    "last_seen_age": max(0.0, now - last_seen),
                    "last_sample_ts": row["last_sample_ts"],
                    "latitude": sample.get("latitude"),
                    "longitude": sample.get("longitude"),
                    "baro_altitude": sample.get("baro_altitude"),
                    "velocity": sample.get("velocity"),
                    "vertical_rate": sample.get("vertical_rate"),
                }
            )
        today = datetime.fromtimestamp(now).date()
        events_today = view.events_today if view.event_day == today else 0
        return {
            "airport": code,
            "name": view.name,
            "poll_time": view.poll_time,
            "polled_at": view.polled_at,
            "credits_remaining": view.credits_remaining,
            "aircraft_count": view.aircraft_count,
            "last_error": view.last_error,
            "started_at": self._started_at,
            "generated_at": now,
            "counts": _counts(
                len(tracks),
                on_ground,
                airborne,
                pending,
                events_today,
                view.events_since_start,
            ),
            "tracks": tracks,
            "events": [dict(event) for event in view.events],
        }


def _counts(
    tracked: int,
    on_ground: int,
    airborne: int,
    pending: int,
    events_today: int,
    events_since_start: int,
) -> dict[str, int]:
    return {
        "tracked": tracked,
        "on_ground": on_ground,
        "airborne": airborne,
        "pending": pending,
        "events_today": events_today,
        "events_since_start": events_since_start,
    }


def _position(sample: AircraftSample) -> dict[str, object]:
    return {
        "latitude": sample.latitude,
        "longitude": sample.longitude,
        "baro_altitude": sample.baro_altitude,
        "velocity": sample.velocity,
        "vertical_rate": sample.vertical_rate,
    }


def _airport_code(value: str) -> str:
    code = value.strip().upper()
    if not code:
        raise ValueError("airport is required")
    return code


def _clean_error(message: str) -> str:
    text = " ".join(message.split())
    if not text:
        return "request failed"
    if len(text) > _ERROR_LIMIT:
        return text[: _ERROR_LIMIT - 3] + "..."
    return text
