"""Parser and OpenSky client. HTTP is mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from airport_monitor.airports import ZGSZ
from airport_monitor.ingest import (
    OpenSkyClient,
    OpenSkyError,
    OpenSkyRateLimitError,
    TokenManager,
    minimum_sustainable_interval,
    parse_state_response,
    state_request_credit_cost,
)
from airport_monitor.models import BoundingBox


def row(
    *,
    icao: str = "780ABC",
    callsign: str | None = "CSZ9001 ",
    on_ground: object = False,
    lat: float = 22.64,
    lon: float = 113.81,
    last_contact: int = 1_700_000_000,
    baro: float = 30.0,
    velocity: float = 70.0,
    vertical_rate: float = -1.5,
) -> list[object]:
    return [
        icao,
        callsign,
        "China",
        last_contact,
        last_contact,
        lon,
        lat,
        baro,
        on_ground,
        velocity,
        150.0,
        vertical_rate,
        None,
        40.0,
        "1234",
        False,
        0,
    ]


def test_parse_keeps_documented_indexes_and_strips_callsign() -> None:
    snapshot = parse_state_response(
        {"time": 1_700_000_010, "states": [row()]},
        fallback_ts=1,
    )
    assert snapshot.time == 1_700_000_010
    assert len(snapshot.aircraft) == 1
    sample = snapshot.aircraft[0]
    assert sample.icao24 == "780abc"
    assert sample.callsign == "CSZ9001"
    assert sample.on_ground is False
    assert sample.latitude == pytest.approx(22.64)
    assert sample.longitude == pytest.approx(113.81)
    assert sample.baro_altitude == pytest.approx(30.0)
    assert sample.velocity == pytest.approx(70.0)
    assert sample.vertical_rate == pytest.approx(-1.5)
    assert sample.last_contact == 1_700_000_000
    assert sample.ts == 1_700_000_010


def test_null_or_missing_states_are_an_empty_box() -> None:
    for payload in ({"time": 5, "states": None}, {"time": 5}):
        snapshot = parse_state_response(payload, fallback_ts=9)
        assert snapshot.time == 5
        assert snapshot.aircraft == ()


def test_missing_time_uses_fallback() -> None:
    snapshot = parse_state_response({"states": None}, fallback_ts=42)
    assert snapshot.time == 42


def test_bad_document_shapes_raise() -> None:
    with pytest.raises(OpenSkyError):
        parse_state_response(["nope"], fallback_ts=1)
    with pytest.raises(OpenSkyError):
        parse_state_response({"time": 1, "states": {}}, fallback_ts=1)
    with pytest.raises(OpenSkyError):
        parse_state_response({"time": 1.5, "states": []}, fallback_ts=1)


def test_bad_rows_are_skipped_and_duplicate_icao_keeps_the_last() -> None:
    short = row()[:8]
    snapshot = parse_state_response(
        {
            "time": 10,
            "states": [
                short,
                row(on_ground=0),
                row(on_ground=False, callsign=None),
                "not-a-row",
                row(on_ground=True, callsign="CSZ1"),
                row(icao="781111", on_ground=False),
            ],
        },
        fallback_ts=1,
    )
    by_id = {sample.icao24: sample for sample in snapshot.aircraft}
    assert set(by_id) == {"780abc", "781111"}
    assert by_id["780abc"].on_ground is True
    assert by_id["780abc"].callsign == "CSZ1"


def test_zgsz_box_costs_one_credit_and_intervals_match_the_budget() -> None:
    assert state_request_credit_cost(ZGSZ.box) == 1
    assert minimum_sustainable_interval(1, authenticated=False) == 216
    assert minimum_sustainable_interval(1, authenticated=True) == 22

    def box_for(area: float) -> BoundingBox:
        height = 10.0
        return BoundingBox(lamin=0, lamax=height, lomin=0, lomax=area / height)

    assert state_request_credit_cost(box_for(25)) == 1
    assert state_request_credit_cost(box_for(25.01)) == 2
    assert state_request_credit_cost(box_for(100)) == 2
    assert state_request_credit_cost(box_for(100.01)) == 3
    assert state_request_credit_cost(box_for(400)) == 3
    assert state_request_credit_cost(box_for(400.01)) == 4


def _client(handler, tokens: TokenManager | None = None) -> tuple[httpx.Client, OpenSkyClient]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenSkyClient(
        http=http,
        base_url="https://opensky-network.org/api",
        token_manager=tokens,
    )
    return http, client


def test_fetch_sends_the_box_and_reads_remaining_credits() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["ua"] = request.headers["user-agent"]
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            headers={"X-Rate-Limit-Remaining": "399"},
            json={"time": 1_700_000_000, "states": [row()]},
        )

    http, client = _client(handler)
    with http:
        snapshot = client.fetch(ZGSZ.box)
    assert seen["params"] == {
        "lamin": "22.5800",
        "lomin": "113.7400",
        "lamax": "22.7000",
        "lomax": "113.8800",
    }
    assert str(seen["ua"]).startswith("airport-monitor/")
    assert seen["auth"] is None
    assert snapshot.credits_remaining == 399
    assert snapshot.aircraft[0].on_ground is False


def test_unparseable_credit_header_is_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Rate-Limit-Remaining": "nope"},
            json={"time": 1, "states": None},
        )

    http, client = _client(handler)
    with http:
        assert client.fetch(ZGSZ.box).credits_remaining is None


def test_rate_limit_reads_opensky_header_then_retry_after() -> None:
    def limited(header: str, value: str) -> OpenSkyRateLimitError:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={header: value}, json={"message": "slow"})

        http, client = _client(handler)
        with http:
            with pytest.raises(OpenSkyRateLimitError) as caught:
                client.fetch(ZGSZ.box)
        return caught.value

    assert limited("X-Rate-Limit-Retry-After-Seconds", "12.5").retry_after == pytest.approx(12.5)
    assert limited("Retry-After", "8").retry_after == pytest.approx(8)

    def bare(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "soon"})

    http, client = _client(bare)
    with http:
        with pytest.raises(OpenSkyRateLimitError) as caught:
            client.fetch(ZGSZ.box)
    assert caught.value.retry_after is None


def test_403_and_transport_errors_are_opensky_errors() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    http, client = _client(forbidden)
    with http:
        with pytest.raises(OpenSkyError) as caught:
            client.fetch(ZGSZ.box)
    assert caught.value.status_code == 403

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    http, client = _client(down)
    with http:
        with pytest.raises(OpenSkyError, match="failed"):
            client.fetch(ZGSZ.box)


def test_token_is_cached_then_refreshed_and_401_retries_once() -> None:
    clock_now = datetime(2026, 1, 1, tzinfo=UTC)
    posts = {"token": 0, "states": 0}
    expected_bearer = {"value": "Bearer tok-2"}

    def clock() -> datetime:
        return clock_now

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            posts["token"] += 1
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "client_id=test-id" in body
            return httpx.Response(
                200, json={"access_token": f"tok-{posts['token']}", "expires_in": 1800}
            )
        posts["states"] += 1
        if posts["states"] == 1:
            return httpx.Response(401, json={"error": "expired"})
        assert request.headers["authorization"] == expected_bearer["value"]
        return httpx.Response(200, json={"time": 10, "states": None})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        tokens = TokenManager(
            client_id="test-id",
            client_secret="test-secret",
            http=http,
            token_url="https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
            clock=clock,
        )
        client = OpenSkyClient(
            http=http,
            base_url="https://opensky-network.org/api",
            token_manager=tokens,
        )
        snapshot = client.fetch(ZGSZ.box)
        assert snapshot.time == 10
        client.fetch(ZGSZ.box)
        assert posts["token"] == 2
        clock_now = clock_now + timedelta(seconds=1770)
        expected_bearer["value"] = "Bearer tok-3"
        client.fetch(ZGSZ.box)
        assert posts["token"] == 3


def test_repeated_401_does_not_loop() -> None:
    posts = {"token": 0, "states": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            posts["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        posts["states"] += 1
        return httpx.Response(401, text="nope")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        tokens = TokenManager(
            client_id="id",
            client_secret="secret",
            http=http,
            token_url="https://auth.example/token",
        )
        client = OpenSkyClient(
            http=http,
            base_url="https://opensky-network.org/api",
            token_manager=tokens,
        )
        with pytest.raises(OpenSkyError) as caught:
            client.fetch(ZGSZ.box)
    assert caught.value.status_code == 401
    assert posts["states"] == 2
    assert posts["token"] == 2
