"""Alert delivery.

The webhook is the primary channel: it POSTs the event JSON. The email
adapter sends the same JSON over SMTP when the mailbox settings are
complete, and otherwise logs the payload without opening a connection.
"""

from __future__ import annotations

import json
import smtplib
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import urlparse

import httpx
from loguru import logger

from airport_monitor.models import AlertEvent

_SMTP_TIMEOUT_SECONDS = 20.0


class AlertDeliveryError(Exception):
    """A sink tried to deliver and failed. The dispatcher isolates these."""


class AlertSink(Protocol):
    def emit(self, event: AlertEvent) -> None: ...


class WebhookAlerter:
    """POST the event JSON. No-op when ``url`` is unset."""

    def __init__(self, url: str | None, http: httpx.Client, *, timeout: float = 10.0) -> None:
        self._url = url
        self._http = http
        self._timeout = timeout

    def describe(self) -> str:
        if not self._url:
            return "disabled"
        parsed = urlparse(self._url)
        if not parsed.scheme or not parsed.netloc:
            return "enabled"
        return f"POST {parsed.scheme}://{parsed.netloc}{parsed.path}"

    def emit(self, event: AlertEvent) -> None:
        if not self._url:
            return
        payload = event.to_payload()
        try:
            response = self._http.post(self._url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise AlertDeliveryError(f"webhook request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise AlertDeliveryError(f"webhook HTTP {response.status_code}: {detail}")
        logger.info("webhook delivered {} {}", event.event, event.icao24)


class EmailAlerter:
    """SMTP adapter. Logs and returns when the mailbox is not fully configured."""

    def __init__(
        self,
        *,
        to: str | None,
        host: str | None,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        use_tls: bool = True,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self._to = to
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._use_tls = use_tls
        self._smtp_factory = smtp_factory

    @property
    def can_send(self) -> bool:
        if not self._to or not self._host or not (self._sender or self._username):
            return False
        if self._username and not self._password:
            return False
        return True

    @property
    def partial(self) -> bool:
        provided = any((self._to, self._host, self._username, self._password, self._sender))
        return provided and not self.can_send

    def describe(self) -> str:
        if self.can_send and self._host and self._to:
            return f"smtp {self._host}:{self._port} to {self._to}"
        if self.partial:
            return (
                "incomplete (need ALERT_EMAIL_TO, SMTP_HOST, and SMTP_FROM "
                "or SMTP_USER plus SMTP_PASSWORD)"
            )
        return "stub (not configured)"

    def emit(self, event: AlertEvent) -> None:
        payload = event.to_payload()
        if not self.can_send:
            logger.info("email stub: {}", payload)
            return
        self._send(event, payload)

    def _send(self, event: AlertEvent, payload: dict[str, object]) -> None:
        sender = self._sender or self._username
        if sender is None or self._host is None or self._to is None:
            logger.info("email stub: {}", payload)
            return
        message = EmailMessage()
        message["Subject"] = f"[{event.airport}] {event.event} {event.callsign or event.icao24}"
        message["From"] = sender
        message["To"] = self._to
        body = json.dumps(payload, indent=2)
        message.set_content(
            f"{event.airport} {event.event} {event.callsign or event.icao24}\n\n{body}\n"
        )
        try:
            with self._smtp_factory(self._host, self._port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
                if self._use_tls:
                    smtp.starttls()
                if self._username:
                    if not self._password:
                        raise AlertDeliveryError("SMTP_PASSWORD is required when SMTP_USER is set")
                    smtp.login(self._username, self._password)
                smtp.send_message(message)
        except AlertDeliveryError:
            raise
        except OSError as exc:
            raise AlertDeliveryError(f"email delivery failed: {exc}") from exc
        logger.info("email sent {} {} to {}", event.event, event.icao24, self._to)


class AlertDispatcher:
    """Fan out one event. A failing sink does not block the sinks after it."""

    def __init__(self, sinks: Sequence[AlertSink]) -> None:
        self._sinks = tuple(sinks)

    @property
    def sinks(self) -> tuple[AlertSink, ...]:
        return self._sinks

    def emit(self, event: AlertEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception as exc:
                logger.opt(exception=True).error(
                    "alert sink {} failed for {} {}: {}",
                    type(sink).__name__,
                    event.event,
                    event.icao24,
                    exc,
                )
