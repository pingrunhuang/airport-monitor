"""CLI: ``uv run python -m airport_monitor``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import httpx
from loguru import logger
from pydantic import ValidationError

from airport_monitor.airports import Airport, UnknownAirportError, resolve_airports
from airport_monitor.alert import AlertDispatcher, EmailAlerter, WebhookAlerter
from airport_monitor.config import Settings, validate_poll_interval
from airport_monitor.ingest import (
    USER_AGENT,
    OpenSkyClient,
    TokenManager,
    daily_credit_spend,
    minimum_sustainable_interval,
    over_daily_credit_budget,
    state_request_credit_cost,
)
from airport_monitor.loop import run_monitor
from airport_monitor.state_machine import GroundStateTracker


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = Settings()
        interval = settings.poll_interval_seconds if args.interval is None else args.interval
        interval = validate_poll_interval(interval, allow_fast=settings.opensky_allow_fast_poll)
        airports = resolve_airports(settings.airport_icao, settings.bounding_box())
    except (ValidationError, ValueError, UnknownAirportError) as exc:
        print(exc, file=sys.stderr)
        return 2

    _configure_logging(settings.log_level)
    authenticated = bool(settings.opensky_client_id and settings.opensky_client_secret)
    _log_plan(settings, airports, interval, authenticated)

    trackers = {
        airport.icao: GroundStateTracker(
            airport=airport.icao,
            debounce_samples=settings.debounce_samples,
            ttl_seconds=settings.track_ttl_seconds,
        )
        for airport in airports
    }
    timeout = httpx.Timeout(settings.http_timeout_seconds)
    with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as http:
        tokens = None
        if authenticated:
            assert settings.opensky_client_id is not None
            assert settings.opensky_client_secret is not None
            tokens = TokenManager(
                client_id=settings.opensky_client_id,
                client_secret=settings.opensky_client_secret,
                http=http,
                token_url=settings.opensky_token_url,
            )
        client = OpenSkyClient(
            http=http,
            base_url=settings.opensky_base_url,
            token_manager=tokens,
        )
        webhook = WebhookAlerter(settings.alert_webhook_url, http)
        email = EmailAlerter(
            to=settings.alert_email_to,
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            sender=settings.smtp_from,
            use_tls=settings.smtp_use_tls,
        )
        logger.info("alerts: webhook={} email={}", webhook.describe(), email.describe())
        if email.partial:
            logger.warning("email adapter will not send: {}", email.describe())
        dispatcher = AlertDispatcher([webhook, email])
        try:
            run_monitor(
                airports=airports,
                client=client,
                trackers=trackers,
                dispatcher=dispatcher,
                poll_interval_seconds=interval,
                max_cycles=1 if args.once else None,
            )
        except KeyboardInterrupt:
            logger.info("stopped")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m airport_monitor",
        description=(
            "Poll OpenSky ADS-B state vectors around an airport and alert "
            "when an aircraft takes off or lands."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (still establishes no alert on a single sample).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Override POLL_INTERVAL_SECONDS. Minimum 10 unless OPENSKY_ALLOW_FAST_POLL=1.",
    )
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
        backtrace=False,
        diagnose=False,
    )


def _log_plan(
    settings: Settings,
    airports: Sequence[Airport],
    interval: float,
    authenticated: bool,
) -> None:
    per_cycle = 0
    for airport in airports:
        box = airport.box
        per_cycle += state_request_credit_cost(box)
        logger.info(
            "monitoring {} ({}, {}) lat {:.2f}-{:.2f} lon {:.2f}-{:.2f}",
            airport.icao,
            airport.name,
            airport.city,
            box.lamin,
            box.lamax,
            box.lomin,
            box.lomax,
        )
    logger.info(
        "poll every {:.0f}s debounce={} ttl={:.0f}s auth={}",
        interval,
        settings.debounce_samples,
        settings.track_ttl_seconds,
        "oauth2" if authenticated else "anonymous",
    )
    spend = sum(daily_credit_spend(interval, airport.box) for airport in airports)
    logger.info(
        "about {:.0f} OpenSky credits/day for {} request(s) per cycle",
        spend,
        len(airports),
    )
    if over_daily_credit_budget(spend, authenticated=authenticated):
        limit = 4000 if authenticated else 400
        tier = "standard OpenSky account" if authenticated else "anonymous access"
        needed = minimum_sustainable_interval(per_cycle, authenticated=authenticated)
        logger.warning(
            "This interval needs about {:.0f} credits/day, above the {} budget of {}. "
            "Use at least {}s between polls for a full day, or wait out HTTP 429. "
            "Active feeders have 8000 credits/day.",
            spend,
            tier,
            limit,
            needed,
        )


if __name__ == "__main__":
    raise SystemExit(main())
