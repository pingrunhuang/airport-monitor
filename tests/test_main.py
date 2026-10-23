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
    monkeypatch.setenv("UI_ENABLED", "0")
    called: dict[str, object] = {}

    def fake_run(**kwargs: object) -> int:
        called.update(kwargs)
        return 1

    def fail_if_blocked() -> None:
        raise AssertionError("ui wait")

    monkeypatch.setattr("airport_monitor.__main__.run_monitor", fake_run)
    monkeypatch.setattr("airport_monitor.__main__.serve_ui", fail_if_blocked)
    monkeypatch.setattr("airport_monitor.__main__._block_until_interrupt", fail_if_blocked)
    assert main(["--once", "--interval", "12"]) == 0
    assert called["max_cycles"] == 1
    assert called["poll_interval_seconds"] == 12
    assert called["board"] is None
    airports = called["airports"]
    assert airports[0].icao == "ZGSZ"  # type: ignore[index]
    assert isinstance(called["client"], OpenSkyClient)
    sinks = called["dispatcher"].sinks  # type: ignore[attr-defined]
    assert isinstance(sinks[0], WebhookAlerter)
    assert isinstance(sinks[1], EmailAlerter)
    assert sinks[0].describe() == "disabled"
    assert sinks[1].can_send is False


def test_once_starts_the_board_before_the_poll_and_keeps_it_up(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UI_ENABLED", "1")
    monkeypatch.setenv("UI_HOST", "127.0.0.1")
    monkeypatch.setenv("UI_PORT", "8799")
    order: list[str] = []
    started: dict[str, object] = {}

    class FakeServer:
        server_address = ("127.0.0.1", 8799)

        def shutdown(self) -> None:
            order.append("stop")

        def server_close(self) -> None:
            order.append("close")

    def fake_serve(board: object, **kwargs: object) -> FakeServer:
        order.append("ui")
        started["board"] = board
        started.update(kwargs)
        return FakeServer()

    def fake_run(**kwargs: object) -> int:
        order.append("run")
        started["run_board"] = kwargs["board"]
        return 1

    monkeypatch.setattr("airport_monitor.__main__.serve_ui", fake_serve)
    monkeypatch.setattr("airport_monitor.__main__.run_monitor", fake_run)
    monkeypatch.setattr(
        "airport_monitor.__main__._block_until_interrupt",
        lambda: order.append("wait"),
    )
    assert main(["--once", "--interval", "12"]) == 0
    assert order == ["ui", "run", "wait", "stop", "close"]
    assert started["run_board"] is started["board"]
    assert started["host"] == "127.0.0.1"
    assert started["port"] == 8799
    assert started["refresh_seconds"] == 10
    assert started["default_lang"] == "zh"


def test_ui_bind_failure_still_polls_once_and_exits(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UI_ENABLED", "1")
    called: dict[str, object] = {}

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("address in use")

    def fake_run(**kwargs: object) -> int:
        called.update(kwargs)
        return 1

    def fail_if_blocked() -> None:
        raise AssertionError("ui wait")

    monkeypatch.setattr("airport_monitor.__main__.serve_ui", boom)
    monkeypatch.setattr("airport_monitor.__main__.run_monitor", fake_run)
    monkeypatch.setattr("airport_monitor.__main__._block_until_interrupt", fail_if_blocked)
    assert main(["--once"]) == 0
    assert called["board"] is None
