"""Settings validation and the env example."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from airport_monitor.airports import ZGSZ
from airport_monitor.config import Settings


def test_defaults_match_the_zgsz_catalog(clean_env: None) -> None:
    settings = Settings(_env_file=None)
    assert settings.airport_icao == "ZGSZ"
    assert settings.bounding_box() == ZGSZ.box
    assert settings.poll_interval_seconds == 15
    assert settings.debounce_samples == 2
    assert settings.opensky_client_id is None
    assert settings.alert_webhook_url is None


def test_interval_below_ten_seconds_is_rejected(clean_env: None) -> None:
    with pytest.raises(ValidationError, match=">= 10"):
        Settings(_env_file=None, poll_interval_seconds=9.9)
    Settings(_env_file=None, poll_interval_seconds=10)


def test_fast_poll_override(clean_env: None) -> None:
    settings = Settings(_env_file=None, poll_interval_seconds=1, opensky_allow_fast_poll=True)
    assert settings.poll_interval_seconds == 1


def test_blank_strings_are_unset_and_credentials_must_be_paired(clean_env: None) -> None:
    settings = Settings(_env_file=None, alert_webhook_url="  ", smtp_password="")
    assert settings.alert_webhook_url is None
    assert settings.smtp_password is None
    with pytest.raises(ValidationError, match="OPENSKY_CLIENT_ID"):
        Settings(_env_file=None, opensky_client_id="client-only")


def test_inverted_box_is_rejected(clean_env: None) -> None:
    with pytest.raises(ValidationError, match="bounding box"):
        Settings(_env_file=None, opensky_lamin=23, opensky_lamax=22)


def test_smtp_tls_flag(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    assert Settings(_env_file=None).smtp_use_tls is False


def test_ui_defaults_and_can_be_disabled(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.ui_enabled is True
    assert settings.ui_host == "127.0.0.1"
    assert settings.ui_port == 8787
    monkeypatch.setenv("UI_ENABLED", "0")
    monkeypatch.setenv("UI_HOST", "0.0.0.0")
    monkeypatch.setenv("UI_PORT", "9001")
    disabled = Settings(_env_file=None)
    assert disabled.ui_enabled is False
    assert disabled.ui_host == "0.0.0.0"
    assert disabled.ui_port == 9001
    with pytest.raises(ValidationError, match="UI_PORT"):
        Settings(_env_file=None, ui_port=0)
    with pytest.raises(ValidationError, match="UI_HOST"):
        Settings(_env_file=None, ui_host=" ")


def test_ui_lang_defaults_to_zh_and_accepts_en(clean_env: None) -> None:
    assert Settings(_env_file=None).ui_lang == "zh"
    assert Settings(_env_file=None, ui_lang="en").ui_lang == "en"
    assert Settings(_env_file=None, ui_lang="zh-CN").ui_lang == "zh"
    assert Settings(_env_file=None, ui_lang="zh_CN").ui_lang == "zh"
    assert Settings(_env_file=None, ui_lang="en-US").ui_lang == "en"
    assert Settings(_env_file=None, ui_lang="  ").ui_lang == "zh"
    with pytest.raises(ValidationError, match="UI_LANG"):
        Settings(_env_file=None, ui_lang="fr")


def test_env_example_has_placeholders_and_no_secrets() -> None:
    text = Path(".env.example").read_text()
    assert "BEGIN PRIVATE" not in text
    required = {
        "AIRPORT_ICAO",
        "OPENSKY_LAMIN",
        "OPENSKY_LAMAX",
        "OPENSKY_LOMIN",
        "OPENSKY_LOMAX",
        "POLL_INTERVAL_SECONDS",
        "DEBOUNCE_SAMPLES",
        "ALERT_WEBHOOK_URL",
        "ALERT_EMAIL_TO",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "OPENSKY_CLIENT_ID",
        "OPENSKY_CLIENT_SECRET",
        "UI_ENABLED",
        "UI_HOST",
        "UI_PORT",
        "UI_LANG",
    }
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        found.add(key)
        if key in {"SMTP_PASSWORD", "OPENSKY_CLIENT_SECRET", "OPENSKY_CLIENT_ID"}:
            assert value.strip() == ""
    assert required <= found
