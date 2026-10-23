"""Isolate settings tests from the developer environment."""

from __future__ import annotations

import pytest

ENV_KEYS = (
    "AIRPORT_ICAO",
    "OPENSKY_BASE_URL",
    "OPENSKY_TOKEN_URL",
    "OPENSKY_LAMIN",
    "OPENSKY_LAMAX",
    "OPENSKY_LOMIN",
    "OPENSKY_LOMAX",
    "POLL_INTERVAL_SECONDS",
    "DEBOUNCE_SAMPLES",
    "TRACK_TTL_SECONDS",
    "OPENSKY_ALLOW_FAST_POLL",
    "HTTP_TIMEOUT_SECONDS",
    "OPENSKY_CLIENT_ID",
    "OPENSKY_CLIENT_SECRET",
    "ALERT_WEBHOOK_URL",
    "ALERT_EMAIL_TO",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_USE_TLS",
    "LOG_LEVEL",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
