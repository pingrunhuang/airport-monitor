"""Shared data types for samples, alerts, and the geographic box."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EventKind = Literal["takeoff", "landing"]


def normalize_icao24(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("icao24 is required")
    return normalized


def normalize_callsign(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class BoundingBox(BaseModel):
    """WGS84 box passed to OpenSky as lamin, lomin, lamax, and lomax."""

    model_config = ConfigDict(frozen=True)

    lamin: float
    lamax: float
    lomin: float
    lomax: float

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBox:
        if not -90.0 <= self.lamin < self.lamax <= 90.0:
            raise ValueError("latitude bounds must satisfy -90 <= lamin < lamax <= 90")
        if not -180.0 <= self.lomin < self.lomax <= 180.0:
            raise ValueError("longitude bounds must satisfy -180 <= lomin < lomax <= 180")
        return self

    def contains(self, latitude: float, longitude: float) -> bool:
        return self.lamin <= latitude <= self.lamax and self.lomin <= longitude <= self.lomax

    @property
    def area_sq_deg(self) -> float:
        return (self.lamax - self.lamin) * (self.lomax - self.lomin)

    def as_query(self) -> dict[str, str]:
        return {
            "lamin": f"{self.lamin:.4f}",
            "lomin": f"{self.lomin:.4f}",
            "lamax": f"{self.lamax:.4f}",
            "lomax": f"{self.lomax:.4f}",
        }


class AircraftSample(BaseModel):
    """One aircraft as seen in a single OpenSky poll.

    ``ts`` is the response timestamp for that poll, not ``last_contact``.
    Debounce counts polls, so every aircraft in one response shares a ``ts``.
    """

    model_config = ConfigDict(frozen=True)

    icao24: str
    callsign: str | None
    on_ground: bool
    ts: int
    latitude: float | None = None
    longitude: float | None = None
    baro_altitude: float | None = None
    velocity: float | None = None
    vertical_rate: float | None = None
    last_contact: int | None = None

    @field_validator("icao24")
    @classmethod
    def _icao24(cls, value: str) -> str:
        return normalize_icao24(value)

    @field_validator("callsign")
    @classmethod
    def _callsign(cls, value: str | None) -> str | None:
        return normalize_callsign(value)

    @field_validator("ts")
    @classmethod
    def _ts(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ts must be a unix timestamp")
        return value


class AlertEvent(BaseModel):
    """JSON body posted to the webhook and included in the email.

    Keys stay in this order: airport, event, icao24, callsign, ts.
    ``callsign`` is JSON null when OpenSky has not reported one.
    """

    model_config = ConfigDict(frozen=True)

    airport: str
    event: EventKind
    icao24: str
    callsign: str | None
    ts: int

    @field_validator("airport")
    @classmethod
    def _airport(cls, value: str) -> str:
        code = value.strip().upper()
        if not code:
            raise ValueError("airport is required")
        return code

    @field_validator("icao24")
    @classmethod
    def _icao24(cls, value: str) -> str:
        return normalize_icao24(value)

    @field_validator("callsign")
    @classmethod
    def _callsign(cls, value: str | None) -> str | None:
        return normalize_callsign(value)

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class Snapshot(BaseModel):
    """One successful ``GET /states/all`` response, after parsing."""

    model_config = ConfigDict(frozen=True)

    time: int
    aircraft: tuple[AircraftSample, ...] = Field(default_factory=tuple)
    credits_remaining: int | None = None
