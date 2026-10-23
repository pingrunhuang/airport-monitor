"""Environment configuration.

Run from the repository root so ``.env`` is picked up. Empty strings are
treated as unset, which keeps ``.env.example`` safe to copy.
"""

from __future__ import annotations

import math

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from airport_monitor.airports import ZGSZ
from airport_monitor.models import BoundingBox

MIN_POLL_INTERVAL_SECONDS = 10.0

_OPTIONAL_STRINGS = (
    "opensky_client_id",
    "opensky_client_secret",
    "alert_webhook_url",
    "alert_email_to",
    "smtp_host",
    "smtp_user",
    "smtp_password",
    "smtp_from",
)

_LOG_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}


def validate_poll_interval(seconds: float, *, allow_fast: bool) -> float:
    """Reject intervals that would poll inside OpenSky's 10 second bucket.

    Anonymous state vectors only change every 10 seconds. A faster loop
    spends credits on duplicate snapshots. ``allow_fast`` exists so unit
    tests can run a short interval; set ``OPENSKY_ALLOW_FAST_POLL=1``.
    """

    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS must be a positive finite number")
    if seconds < MIN_POLL_INTERVAL_SECONDS and not allow_fast:
        raise ValueError(
            "POLL_INTERVAL_SECONDS must be >= 10 to match anonymous OpenSky "
            "time resolution (set OPENSKY_ALLOW_FAST_POLL=1 only for tests)"
        )
    return seconds


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    airport_icao: str = "ZGSZ"
    opensky_base_url: str = "https://opensky-network.org/api"
    opensky_token_url: str = (
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    )
    opensky_lamin: float = ZGSZ.box.lamin
    opensky_lamax: float = ZGSZ.box.lamax
    opensky_lomin: float = ZGSZ.box.lomin
    opensky_lomax: float = ZGSZ.box.lomax

    poll_interval_seconds: float = 15.0
    debounce_samples: int = 2
    track_ttl_seconds: float = 900.0
    opensky_allow_fast_poll: bool = False
    http_timeout_seconds: float = 20.0

    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None

    alert_webhook_url: str | None = None
    alert_email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True

    log_level: str = "INFO"

    ui_enabled: bool = True
    ui_host: str = "127.0.0.1"
    ui_port: int = 8787

    @field_validator(*_OPTIONAL_STRINGS, mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("airport_icao")
    @classmethod
    def _airport_icao(cls, value: str) -> str:
        code = value.strip().upper()
        if not code:
            raise ValueError("AIRPORT_ICAO is required")
        return code

    @field_validator("ui_host")
    @classmethod
    def _ui_host(cls, value: str) -> str:
        host = value.strip()
        if not host or any(char.isspace() for char in host):
            raise ValueError("UI_HOST must be a hostname or IP address")
        return host

    @field_validator("ui_port")
    @classmethod
    def _ui_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("UI_PORT must be between 1 and 65535")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _LOG_LEVELS:
            allowed = ", ".join(sorted(_LOG_LEVELS))
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return normalized

    @field_validator("opensky_base_url", "opensky_token_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        stripped = value.strip().rstrip("/")
        if not stripped:
            raise ValueError("URL setting must not be empty")
        return stripped

    @model_validator(mode="after")
    def _check(self) -> Settings:
        validate_poll_interval(
            self.poll_interval_seconds,
            allow_fast=self.opensky_allow_fast_poll,
        )
        if self.debounce_samples < 1:
            raise ValueError("DEBOUNCE_SAMPLES must be >= 1")
        if not math.isfinite(self.track_ttl_seconds) or self.track_ttl_seconds <= 0:
            raise ValueError("TRACK_TTL_SECONDS must be positive")
        if not math.isfinite(self.http_timeout_seconds) or self.http_timeout_seconds <= 0:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be positive")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        id_set = self.opensky_client_id is not None
        secret_set = self.opensky_client_secret is not None
        if id_set != secret_set:
            raise ValueError(
                "OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET must both be set or both be empty"
            )
        try:
            self.bounding_box()
        except ValidationError as exc:
            raise ValueError(f"invalid OpenSky bounding box: {exc}") from exc
        return self

    def bounding_box(self) -> BoundingBox:
        return BoundingBox(
            lamin=self.opensky_lamin,
            lamax=self.opensky_lamax,
            lomin=self.opensky_lomin,
            lomax=self.opensky_lomax,
        )
