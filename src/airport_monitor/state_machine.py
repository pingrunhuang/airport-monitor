"""Per-icao24 ``on_ground`` edge detector.

OpenSky state index 8 is ``on_ground`` (already parsed to a bool by ingest):

* ``true`` → ``false`` is a takeoff
* ``false`` → ``true`` is a landing

The new value must hold for ``debounce_samples`` consecutive polls whose
OpenSky timestamps strictly increase. Establishing the baseline is not an
event. A repeated timestamp is the same snapshot and does not count twice.

Altitude and vertical rate are intentionally unused. A later refinement
could require them; the MVP contract is the on-ground edge only.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from airport_monitor.models import AircraftSample, AlertEvent, EventKind


@dataclass(slots=True)
class _Track:
    confirmed: bool | None = None
    pending: bool | None = None
    pending_count: int = 0
    callsign: str | None = None
    last_seen: float = 0.0
    last_sample_ts: int | None = None


class GroundStateTracker:
    """Debounced on-ground transitions for one airport."""

    def __init__(
        self, airport: str, *, debounce_samples: int = 2, ttl_seconds: float = 900.0
    ) -> None:
        if debounce_samples < 1:
            raise ValueError("debounce_samples must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.airport = airport.strip().upper()
        if not self.airport:
            raise ValueError("airport is required")
        self.debounce_samples = debounce_samples
        self.ttl_seconds = ttl_seconds
        self._tracks: dict[str, _Track] = {}

    @property
    def tracked_icao24s(self) -> frozenset[str]:
        return frozenset(self._tracks)

    def observe(self, sample: AircraftSample, *, seen_at: float | None = None) -> AlertEvent | None:
        """Ingest one sample. Return an alert when a debounced edge commits."""

        when = float(sample.ts if seen_at is None else seen_at)
        track = self._tracks.get(sample.icao24)
        if track is None:
            track = _Track()
            self._tracks[sample.icao24] = track
        track.last_seen = when
        if track.last_sample_ts is not None and sample.ts <= track.last_sample_ts:
            logger.debug("{} {} ignored stale ts={}", self.airport, sample.icao24, sample.ts)
            return None
        track.last_sample_ts = sample.ts
        if sample.callsign:
            track.callsign = sample.callsign

        if track.confirmed is None:
            _accumulate(track, sample.on_ground)
            if track.pending_count >= self.debounce_samples:
                track.confirmed = sample.on_ground
                _clear_pending(track)
            self._log_state(sample, track)
            return None

        if sample.on_ground == track.confirmed:
            _clear_pending(track)
            self._log_state(sample, track)
            return None

        _accumulate(track, sample.on_ground)
        if track.pending_count < self.debounce_samples:
            self._log_state(sample, track)
            return None

        previous = track.confirmed
        track.confirmed = sample.on_ground
        _clear_pending(track)
        self._log_state(sample, track)
        if not isinstance(previous, bool):
            return None
        kind = _edge_kind(previous, sample.on_ground)
        if kind is None:
            return None
        return AlertEvent(
            airport=self.airport,
            event=kind,
            icao24=sample.icao24,
            callsign=track.callsign,
            ts=sample.ts,
        )

    def expire(self, now: float) -> int:
        """Drop tracks not seen for longer than ``ttl_seconds``. Return how many."""

        stale = [
            icao for icao, track in self._tracks.items() if now - track.last_seen > self.ttl_seconds
        ]
        for icao in stale:
            del self._tracks[icao]
        return len(stale)

    def _log_state(self, sample: AircraftSample, track: _Track) -> None:
        logger.debug(
            "{} {} on_ground={} confirmed={} pending={} count={}",
            self.airport,
            sample.icao24,
            sample.on_ground,
            track.confirmed,
            track.pending,
            track.pending_count,
        )


def _accumulate(track: _Track, on_ground: bool) -> None:
    if track.pending == on_ground:
        track.pending_count += 1
        return
    track.pending = on_ground
    track.pending_count = 1


def _clear_pending(track: _Track) -> None:
    track.pending = None
    track.pending_count = 0


def _edge_kind(previous: bool, on_ground: bool) -> EventKind | None:
    if previous is True and on_ground is False:
        return "takeoff"
    if previous is False and on_ground is True:
        return "landing"
    return None
