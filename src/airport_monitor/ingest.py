"""OpenSky ``GET /states/all`` client and state-vector parser.

State-vector indexes follow the OpenSky REST docs. Index 8 is ``on_ground``.
Authentication, when configured, is the OAuth2 client-credentials flow.
Anonymous requests send no Authorization header.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from loguru import logger
from pydantic import ValidationError

from airport_monitor import __version__
from airport_monitor.models import AircraftSample, BoundingBox, Snapshot

USER_AGENT = f"airport-monitor/{__version__} (zgsz-takeoff-landing)"

# https://openskynetwork.github.io/opensky-api/rest.html
_IDX_ICAO24 = 0
_IDX_CALLSIGN = 1
_IDX_LAST_CONTACT = 4
_IDX_LONGITUDE = 5
_IDX_LATITUDE = 6
_IDX_BARO_ALTITUDE = 7
_IDX_ON_GROUND = 8
_IDX_VELOCITY = 9
_IDX_VERTICAL_RATE = 11
_MIN_FIELDS = _IDX_ON_GROUND + 1

ANONYMOUS_DAILY_CREDITS = 400
STANDARD_DAILY_CREDITS = 4_000
FEEDER_DAILY_CREDITS = 8_000
_SECONDS_PER_DAY = 86_400

_TOKEN_REFRESH_MARGIN = timedelta(seconds=30)


class OpenSkyError(Exception):
    """The OpenSky request failed or the body was not a state-vector document."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenSkyRateLimitError(OpenSkyError):
    """HTTP 429. ``retry_after`` is seconds, when OpenSky sent a hint."""

    def __init__(self, message: str, *, retry_after: float | None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def state_request_credit_cost(box: BoundingBox) -> int:
    """Credits for one ``/states/all`` call, from the OpenSky area table.

    Area in square degrees is latitude span times longitude span.
    """

    area = box.area_sq_deg
    if area <= 25:
        return 1
    if area <= 100:
        return 2
    if area <= 400:
        return 3
    return 4


def daily_credit_spend(interval_seconds: float, box: BoundingBox) -> float:
    if interval_seconds <= 0:
        raise ValueError("interval must be positive")
    return (_SECONDS_PER_DAY / interval_seconds) * state_request_credit_cost(box)


def over_daily_credit_budget(spend: float, *, authenticated: bool) -> bool:
    limit = STANDARD_DAILY_CREDITS if authenticated else ANONYMOUS_DAILY_CREDITS
    return spend > limit


def minimum_sustainable_interval(cost_per_cycle: int, *, authenticated: bool) -> int:
    """Smallest whole-second interval that stays inside the documented daily budget.

    Active feeders have a higher budget than ``authenticated`` accounts.
    This uses the anonymous or standard-account figure.
    """

    if cost_per_cycle < 1:
        raise ValueError("cost_per_cycle must be >= 1")
    limit = STANDARD_DAILY_CREDITS if authenticated else ANONYMOUS_DAILY_CREDITS
    return math.ceil(_SECONDS_PER_DAY * cost_per_cycle / limit)


class TokenManager:
    """OAuth2 client-credentials tokens for the OpenSky REST API.

    Tokens last about 30 minutes. This refreshes shortly before expiry and
    on ``invalidate`` after a 401. The secret is never logged.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        http: httpx.Client,
        token_url: str,
        clock: Clock | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._token_url = token_url
        self._clock = clock or _utc_now
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = None

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def get_token(self) -> str:
        now = self._clock()
        if self._token is not None and self._expires_at is not None and now < self._expires_at:
            return self._token
        return self._refresh()

    def _refresh(self) -> str:
        try:
            response = self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise OpenSkyError(f"OpenSky token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise OpenSkyError(
                f"OpenSky token request failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenSkyError("OpenSky token response was not JSON") from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise OpenSkyError("OpenSky token response did not include access_token")
        expires_in = body.get("expires_in", 1800)
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = 1800.0
        lifetime = max(lifetime - _TOKEN_REFRESH_MARGIN.total_seconds(), 1.0)
        self._token = token
        self._expires_at = self._clock() + timedelta(seconds=lifetime)
        logger.debug("OpenSky OAuth2 token refreshed")
        return token


class OpenSkyClient:
    """Fetch current state vectors inside a bounding box."""

    def __init__(
        self,
        *,
        http: httpx.Client,
        base_url: str,
        token_manager: TokenManager | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._tokens = token_manager
        self._user_agent = user_agent

    def fetch(self, box: BoundingBox) -> Snapshot:
        response = self._get(box, force_refresh=False)
        if response.status_code == 401 and self._tokens is not None:
            logger.info("OpenSky returned 401; refreshing the OAuth2 token")
            response = self._get(box, force_refresh=True)
        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenSkyError(
                "OpenSky response was not JSON",
                status_code=response.status_code,
            ) from exc
        snapshot = parse_state_response(payload, fallback_ts=_unix_now())
        return snapshot.model_copy(
            update={"credits_remaining": _credits_remaining(response.headers)}
        )

    def _get(self, box: BoundingBox, *, force_refresh: bool) -> httpx.Response:
        headers = {"Accept": "application/json", "User-Agent": self._user_agent}
        if self._tokens is not None:
            if force_refresh:
                self._tokens.invalidate()
            headers.update(self._tokens.auth_headers())
        try:
            return self._http.get(
                f"{self._base_url}/states/all",
                params=box.as_query(),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise OpenSkyError(f"OpenSky request failed: {exc}") from exc


def parse_state_response(payload: object, *, fallback_ts: int) -> Snapshot:
    """Turn an OpenSky JSON document into samples.

    Rows with a missing or non-boolean ``on_ground`` are skipped. A null
    ``states`` array means the box is empty, not an error. Duplicate
    ``icao24`` values keep the last row in the response.
    """

    if not isinstance(payload, dict):
        raise OpenSkyError("OpenSky payload was not a JSON object")
    snapshot_time = _snapshot_time(payload, fallback_ts)
    states = payload.get("states")
    if states is None:
        states = []
    if not isinstance(states, list):
        raise OpenSkyError("OpenSky payload states was not a list")

    by_icao: dict[str, AircraftSample] = {}
    for row in states:
        sample = _parse_row(row, snapshot_time)
        if sample is not None:
            by_icao[sample.icao24] = sample
    return Snapshot(time=snapshot_time, aircraft=tuple(by_icao.values()))


def _snapshot_time(payload: dict, fallback_ts: int) -> int:
    if "time" not in payload or payload["time"] is None:
        return fallback_ts
    raw_time = payload["time"]
    if isinstance(raw_time, bool) or not isinstance(raw_time, int):
        raise OpenSkyError("OpenSky payload time was not an integer")
    if raw_time < 0:
        raise OpenSkyError("OpenSky payload time was negative")
    return raw_time


def _parse_row(row: object, snapshot_time: int) -> AircraftSample | None:
    if not isinstance(row, list) or len(row) < _MIN_FIELDS:
        logger.debug("skipping short OpenSky state vector")
        return None
    on_ground = row[_IDX_ON_GROUND]
    if not isinstance(on_ground, bool):
        logger.debug("skipping state vector with non-boolean on_ground")
        return None
    icao_raw = row[_IDX_ICAO24]
    if not isinstance(icao_raw, str):
        logger.debug("skipping state vector with a missing icao24")
        return None
    callsign_raw = _at(row, _IDX_CALLSIGN)
    callsign = callsign_raw if isinstance(callsign_raw, str) else None
    try:
        return AircraftSample(
            icao24=icao_raw,
            callsign=callsign,
            on_ground=on_ground,
            ts=snapshot_time,
            latitude=_optional_float(_at(row, _IDX_LATITUDE)),
            longitude=_optional_float(_at(row, _IDX_LONGITUDE)),
            baro_altitude=_optional_float(_at(row, _IDX_BARO_ALTITUDE)),
            velocity=_optional_float(_at(row, _IDX_VELOCITY)),
            vertical_rate=_optional_float(_at(row, _IDX_VERTICAL_RATE)),
            last_contact=_optional_int(_at(row, _IDX_LAST_CONTACT)),
        )
    except ValidationError:
        logger.debug("skipping unreadable state vector for {}", icao_raw)
        return None


def _at(row: list, index: int) -> object:
    if index >= len(row):
        return None
    return row[index]


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code == 429:
        raise OpenSkyRateLimitError(
            "OpenSky credit budget exhausted (HTTP 429)",
            retry_after=_retry_after_seconds(response.headers),
        )
    if response.status_code == 403:
        raise OpenSkyError(
            "OpenSky returned 403. Anonymous access from some cloud networks is "
            "blocked; set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET, or poll "
            "from an allowed network.",
            status_code=403,
        )
    detail = response.text[:300].replace("\n", " ")
    raise OpenSkyError(
        f"OpenSky HTTP {response.status_code}: {detail}", status_code=response.status_code
    )


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    for key in ("X-Rate-Limit-Retry-After-Seconds", "Retry-After"):
        raw = headers.get(key)
        if raw is None or raw.strip() == "":
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def _credits_remaining(headers: httpx.Headers) -> int | None:
    raw = headers.get("X-Rate-Limit-Remaining")
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _unix_now() -> int:
    return int(_utc_now().timestamp())
