"""CLI wiring without calling OpenSky."""

from __future__ import annotations

from pathlib import Path

import pytest

from airport_monitor.__main__ import main
from airport_monitor.alert import EmailAlerter, WebhookAlerter
from airport_monitor.ingest import OpenSkyClient


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0


def test_fast_interval_is_rejected(clean_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--interval", "1"]) == 2
    assert "10" in capsys.readouterr().err


def test_unknown_airport_exits_with_the_phase_2_hint(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIRPORT_ICAO", "ZZZZ")
    assert main(["--once"]) == 2
    assert "Phase 2" in capsys.readouterr().err


def test_once_wires_webhook_then_email_and_does_not_poll(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    called: dict[str, object] = {}

    def fake_run(**kwargs: object) -> int:
        called.update(kwargs)
        return 1

    monkeypatch.setattr("airport_monitor.__main__.run_monitor", fake_run)
    assert main(["--once", "--interval", "12"]) == 0
    assert called["max_cycles"] == 1
    assert called["poll_interval_seconds"] == 12
    airports = called["airports"]
    assert airports[0].icao == "ZGSZ"  # type: ignore[index]
    assert isinstance(called["client"], OpenSkyClient)
    sinks = called["dispatcher"].sinks  # type: ignore[attr-defined]
    assert isinstance(sinks[0], WebhookAlerter)
    assert isinstance(sinks[1], EmailAlerter)
    assert sinks[0].describe() == "disabled"
    assert sinks[1].can_send is False
