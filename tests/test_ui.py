"""Local board HTTP handler. The client talks to 127.0.0.1, never OpenSky."""

from __future__ import annotations

import httpx

from airport_monitor.live import LiveBoard
from airport_monitor.models import AircraftSample, AlertEvent, Snapshot
from airport_monitor.state_machine import GroundStateTracker
from airport_monitor.ui import index_html, render_index, serve_ui, stop_ui, ui_refresh_seconds


def test_refresh_interval_stays_between_five_and_ten_seconds() -> None:
    assert ui_refresh_seconds(1) == 5
    assert ui_refresh_seconds(7) == 7
    assert ui_refresh_seconds(10) == 10
    assert ui_refresh_seconds(15) == 10
    assert ui_refresh_seconds(240) == 10
    assert ui_refresh_seconds(0) == 5


def test_page_and_status_round_trip() -> None:
    board = LiveBoard()
    board.register("ZGSZ", name="Shenzhen Bao'an International Airport")
    tracker = GroundStateTracker("ZGSZ", debounce_samples=1)
    sample = AircraftSample(
        icao24="780abc",
        callsign="CSZ9001",
        on_ground=False,
        ts=100,
        latitude=22.64,
        longitude=113.81,
        baro_altitude=450.0,
        velocity=70.0,
    )
    tracker.observe(sample, seen_at=1_000.0)
    board.record_poll(
        "ZGSZ",
        Snapshot(time=100, aircraft=(sample,), credits_remaining=42),
        tracker,
        now=1_000.0,
    )
    board.record_events(
        "ZGSZ",
        [AlertEvent(airport="ZGSZ", event="takeoff", icao24="780abc", callsign="CSZ9001", ts=100)],
    )
    server = serve_ui(board, host="127.0.0.1", port=0, refresh_seconds=5)
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=5) as client:
            page = client.get(base + "/")
            english = client.get(base + "/", params={"lang": "en"})
            alias = client.get(base + "/", params={"lang": "zh-CN"})
            ignored = client.get(base + "/", params={"lang": "fr"})
            status = client.get(base + "/api/status")
            scoped = client.get(base + "/api/status", params={"airport": "zgsz"})
            missing = client.get(base + "/map")
    finally:
        stop_ui(server)

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    assert html == index_html().decode("utf-8")
    assert html == render_index("zh").decode("utf-8")
    assert "#0B1F33" in html
    assert "#F4F7FA" in html
    assert "#3B82C4" in html
    assert "#5BA3D9" in html
    assert '<html lang="zh-CN" data-default-lang="zh-CN">' in html
    assert '<span class="title" data-i18n="title">机场监视</span>' in html
    assert "数据来自 " in html
    assert "并非空中交通管制数据。" in html
    assert 'id="lang-zh"' in html
    assert 'id="lang-en"' in html
    assert "airport-monitor-lang" in html
    assert "Airport monitor" in html
    assert "Not an air traffic control feed." in html
    assert "Takeoff" in html
    assert "起飞" in html
    assert 'href="https://opensky-network.org/"' in html
    assert "OpenSky Network" in html
    assert "/api/status" in html
    assert "<img" not in html
    assert "opensky-logo" not in html.lower()
    assert page.headers["cache-control"] == "no-store"
    assert 'data-default-lang="en"' in english.text
    assert 'lang="en"' in english.text
    assert "机场监视" in english.text
    assert alias.text == html
    assert 'data-default-lang="zh-CN"' in ignored.text

    assert status.status_code == 200
    assert "application/json" in status.headers["content-type"]
    body = status.json()
    assert body["airport"] == "ZGSZ"
    assert body["name"].startswith("Shenzhen Bao'an")
    assert body["refresh_seconds"] == 5
    assert body["counts"]["tracked"] == 1
    assert body["counts"]["airborne"] == 1
    assert body["tracks"][0]["icao24"] == "780abc"
    assert body["tracks"][0]["latitude"] == 22.64
    assert body["events"][0]["event"] == "takeoff"
    assert scoped.json()["airport"] == "ZGSZ"
    assert missing.status_code == 404
    assert missing.text == "not found\n"


def test_ui_lang_en_is_the_served_default_until_query_overrides() -> None:
    board = LiveBoard()
    server = serve_ui(board, host="127.0.0.1", port=0, refresh_seconds=5, default_lang="en")
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=5) as client:
            page = client.get(base + "/")
            zh = client.get(base + "/", params={"lang": "zh"})
            status = client.get(base + "/api/status")
    finally:
        stop_ui(server)

    assert page.text == render_index("en").decode("utf-8")
    assert '<html lang="en" data-default-lang="en">' in page.text
    assert "机场监视" in page.text
    assert '<html lang="zh-CN" data-default-lang="zh-CN">' in zh.text
    body = status.json()
    assert body["counts"]["tracked"] == 0
    assert body["events"] == []
