"""Webhook JSON and the SMTP stub."""

from __future__ import annotations

import json

import httpx
import pytest

from airport_monitor.alert import (
    AlertDeliveryError,
    AlertDispatcher,
    EmailAlerter,
    WebhookAlerter,
)
from airport_monitor.models import AlertEvent


def event() -> AlertEvent:
    return AlertEvent(
        airport="zgsz",
        event="landing",
        icao24="780ABC",
        callsign="CSZ3102",
        ts=1_700_000_100,
    )


def test_webhook_posts_the_contract_json() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        seen["type"] = request.headers["content-type"]
        seen["path"] = request.url.path
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        WebhookAlerter("https://hooks.example/zgsz?token=secret", http).emit(event())

    assert seen["path"] == "/zgsz"
    assert str(seen["type"]).startswith("application/json")
    assert seen["body"] == {
        "airport": "ZGSZ",
        "event": "landing",
        "icao24": "780abc",
        "callsign": "CSZ3102",
        "ts": 1_700_000_100,
    }
    assert list(seen["body"]) == ["airport", "event", "icao24", "callsign", "ts"]  # type: ignore[arg-type]


def test_disabled_webhook_does_not_call_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        alerter = WebhookAlerter(None, http)
        assert alerter.describe() == "disabled"
        alerter.emit(event())


def test_webhook_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AlertDeliveryError, match="500"):
            WebhookAlerter("https://hooks.example/zgsz", http).emit(event())


def test_webhook_describe_hides_the_query_string() -> None:
    with httpx.Client() as http:
        text = WebhookAlerter("https://hooks.example/zgsz?token=secret", http).describe()
    assert "secret" not in text
    assert text == "POST https://hooks.example/zgsz"


class _FakeSMTP:
    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = False
        self.logged: tuple[str, str] | None = None
        self.message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.logged = (user, password)

    def send_message(self, message: object) -> None:
        self.message = message


def test_email_stub_does_not_connect_when_unconfigured() -> None:
    def factory(*args: object, **kwargs: object) -> _FakeSMTP:
        raise AssertionError("SMTP should not be opened")

    alerter = EmailAlerter(to=None, host=None, smtp_factory=factory)
    assert alerter.describe() == "stub (not configured)"
    alerter.emit(event())


def test_partial_email_config_stays_a_stub() -> None:
    def factory(*args: object, **kwargs: object) -> _FakeSMTP:
        raise AssertionError("SMTP should not be opened")

    alerter = EmailAlerter(to="ops@example.com", host=None, smtp_factory=factory)
    assert alerter.partial is True
    alerter.emit(event())


def test_email_sends_the_json_payload_when_configured() -> None:
    _FakeSMTP.instances.clear()
    alerter = EmailAlerter(
        to="ops@example.com",
        host="smtp.example",
        port=587,
        username="mailer",
        password="secret",
        sender="monitor@example.com",
        use_tls=True,
        smtp_factory=_FakeSMTP,
    )
    assert alerter.can_send is True
    alerter.emit(event())
    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.example"
    assert smtp.port == 587
    assert smtp.tls is True
    assert smtp.logged == ("mailer", "secret")
    message = smtp.message
    assert message is not None
    assert message["Subject"] == "[ZGSZ] landing CSZ3102"  # type: ignore[index]
    assert message["From"] == "monitor@example.com"  # type: ignore[index]
    body = message.get_content()  # type: ignore[attr-defined]
    assert '"event": "landing"' in body
    assert '"icao24": "780abc"' in body


def test_email_without_tls_or_login_when_those_are_unset() -> None:
    _FakeSMTP.instances.clear()
    alerter = EmailAlerter(
        to="ops@example.com",
        host="smtp.example",
        port=25,
        sender="monitor@example.com",
        use_tls=False,
        smtp_factory=_FakeSMTP,
    )
    alerter.emit(event())
    smtp = _FakeSMTP.instances[0]
    assert smtp.tls is False
    assert smtp.logged is None


def test_dispatcher_continues_after_a_sink_error() -> None:
    class Boom:
        def emit(self, event: AlertEvent) -> None:
            raise RuntimeError("down")

    class Collect:
        def __init__(self) -> None:
            self.events: list[AlertEvent] = []

        def emit(self, event: AlertEvent) -> None:
            self.events.append(event)

    second = Collect()
    AlertDispatcher([Boom(), second]).emit(event())
    assert second.events == [event()]
