"""Local board: one HTML page and ``GET /api/status``.

Stdlib ``ThreadingHTTPServer`` only. The server runs on a daemon thread so
the poll loop stays on the main thread. Bind address and port come from
``UI_HOST`` and ``UI_PORT``.
"""

from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

from loguru import logger

from airport_monitor.live import LiveBoard

_PAGE_CACHE: bytes | None = None


def ui_refresh_seconds(poll_interval_seconds: float) -> float:
    """How often the page fetches ``/api/status``.

    The poll interval is used when it already sits between 5 and 10
    seconds. Slower OpenSky polls still refresh inside that window so the
    age column moves. Faster intervals wait 5 seconds so the page does
    not spin.
    """

    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        return 5.0
    return min(10.0, max(5.0, poll_interval_seconds))


def index_html() -> bytes:
    """The single page, loaded from ``static/index.html`` inside this package."""

    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        _PAGE_CACHE = files("airport_monitor").joinpath("static/index.html").read_bytes()
    return _PAGE_CACHE


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    ui_thread: threading.Thread | None = None


class _BoardHandler(BaseHTTPRequestHandler):
    """GET ``/`` and ``GET /api/status``. Subclasses close over one board."""

    board: LiveBoard
    refresh_seconds: float

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = index_html()
            self._send(200, "text/html; charset=utf-8", body)
            return
        if parsed.path == "/api/status":
            raw = parse_qs(parsed.query).get("airport", [None])[0]
            payload = self.board.status(raw)
            payload["refresh_seconds"] = self.refresh_seconds
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def log_message(self, fmt: str, *args: object) -> None:
        try:
            message = fmt % args
        except (TypeError, ValueError):
            message = fmt
        logger.debug("ui {} {}", self.address_string(), message)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _handler(board: LiveBoard, refresh_seconds: float) -> type[BaseHTTPRequestHandler]:
    class Handler(_BoardHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.board = board
            self.refresh_seconds = refresh_seconds
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    return Handler


def serve_ui(
    board: LiveBoard,
    *,
    host: str,
    port: int,
    refresh_seconds: float,
) -> _Server:
    """Bind and serve on a daemon thread. Return the server so the caller can stop it."""

    if refresh_seconds <= 0 or not math.isfinite(refresh_seconds):
        raise ValueError("refresh_seconds must be positive")
    server = _Server((host, port), _handler(board, refresh_seconds))
    thread = threading.Thread(target=server.serve_forever, name="airport-monitor-ui", daemon=True)
    server.ui_thread = thread
    thread.start()
    return server


def stop_ui(server: ThreadingHTTPServer) -> None:
    """Stop ``serve_forever`` and close the listening socket."""

    server.shutdown()
    server.server_close()
    thread = getattr(server, "ui_thread", None)
    if isinstance(thread, threading.Thread):
        thread.join(timeout=2)
