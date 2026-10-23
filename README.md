# Airport monitor (ZGSZ)

Poll [OpenSky Network](https://opensky-network.org/) ADS-B state vectors around
**Shenzhen Bao'an International Airport (ICAO ZGSZ, IATA SZX)** and emit an
alert when an aircraft takes off or lands.

This is a situational monitor for one airport. It is not an air traffic
control feed. OpenSky data is used under their
[terms of use](https://opensky-network.org/about/terms-of-use).

## What it detects

Each poll reads `GET /api/states/all` inside a box around the runways.
`on_ground` is state-vector index 8:

| Edge | Event |
| --- | --- |
| `true` → `false` | takeoff |
| `false` → `true` | landing |

The new value must show up on **2 consecutive polls** (configurable) with
increasing OpenSky timestamps. The first stable value is only a baseline,
so a cold start does not alert. A single flipped sample is ignored.

With the default 2-sample debounce and a 15 second interval, a real
transition is reported about 30 seconds after it happens, and only if the
aircraft was already baselined inside the box. A touch-and-go shorter than
that window is suppressed on purpose.

There is no camera input and no airline or flight-number lookup.

## Bounding box

Published aerodrome reference point (CAAC AIP): **N22°38.3' E113°48.7'**
(22.6383°N, 113.8117°E). The field has three runways: 15/33, 16L/34R, and
16R/34L.

The default box is:

| | Latitude | Longitude |
| --- | --- | --- |
| min | 22.58 | 113.74 |
| max | 22.70 | 113.88 |

That is about 13 km north–south and 14 km east–west, with roughly 4–6 km
of margin beyond the runway thresholds, and an area of **0.0168 square
degrees**. OpenSky charges **1 credit** for a box at or under 25 square
degrees.

An earlier sketch (lat 22.55–22.70, lon 113.75–113.95) also covers the
airport, but it extends much farther east into the city and leaves less
room west of runway 16R. Override the defaults with `OPENSKY_LAMIN`,
`OPENSKY_LAMAX`, `OPENSKY_LOMIN`, and `OPENSKY_LOMAX` if you want that
wider box. Thresholds used to size the default are in
`src/airport_monitor/airports.py`. They are a coverage check, not a
navigation source.

## Run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
uv run python -m airport_monitor
```

`airport-monitor` is the same entry point. Ctrl-C stops the loop.

One poll, then exit:

```bash
uv run python -m airport_monitor --once
```

The first samples only establish who is on the ground and who is airborne.
Leave it running if you want a takeoff or a landing. A failed poll (timeout,
HTTP 403, HTTP 5xx) is logged in one line and the loop waits for the next
interval.

Logs go to stderr. A quiet poll looks like:

```text
2026-10-23 12:00:15 | INFO    | ZGSZ poll t=1761213610 aircraft=12 events=0 tracked=12 expired=0 credits_remaining=380
```

An alert:

```text
2026-10-23 12:04:00 | INFO    | event {'airport': 'ZGSZ', 'event': 'takeoff', 'icao24': '780abc', 'callsign': 'CSZ9001', 'ts': 1761213840}
```

Set `LOG_LEVEL=DEBUG` to see each track's confirmed and pending `on_ground` value.

## Alerts

Webhook first, then the email adapter. A sink that fails is logged and
does not stop the other one or the poll loop. SMTP is synchronous, so a
slow mail server delays the next poll (the timeout is 20 seconds).

```json
{"airport":"ZGSZ","event":"takeoff","icao24":"780abc","callsign":"CSZ9001","ts":1761213840}
```

`event` is `takeoff` or `landing`. `callsign` is JSON `null` when OpenSky
has none. `ts` is the OpenSky response time (unix seconds) for the poll
that confirmed the edge.

### Webhook

Set `ALERT_WEBHOOK_URL`. The monitor `POST`s the JSON body with
`Content-Type: application/json`. Any HTTP status from 400 up is an error
and is logged.

A local sink on an uncommon port:

```bash
uv run python -c '
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        print(self.rfile.read(size).decode())
        self.send_response(204)
        self.end_headers()
    def log_message(self, fmt, *args):
        return

ThreadingHTTPServer(("127.0.0.1", 8791), Handler).serve_forever()
'
```

```bash
ALERT_WEBHOOK_URL=http://127.0.0.1:8791/hooks/zgsz uv run python -m airport_monitor
```

### Email

The SMTP adapter sends only when all of these are set:

- `ALERT_EMAIL_TO`
- `SMTP_HOST`
- `SMTP_FROM`, or `SMTP_USER` together with `SMTP_PASSWORD`

Otherwise it logs `email stub:` plus the same payload and does not connect.
`SMTP_PORT` defaults to 587 and `SMTP_USE_TLS` defaults to true (STARTTLS).
Set `SMTP_USE_TLS=false` for plain SMTP.

The message subject is `[ZGSZ] takeoff CSZ9001`. The body includes the JSON payload.

## OpenSky rate limits

Documented limits (they change; see the
[REST API docs](https://openskynetwork.github.io/opensky-api/rest.html)):

| Access | Daily credits on `/states/*` | Snapshot resolution |
| --- | --- | --- |
| Anonymous (per IP) | 400 | 10 seconds, current data only |
| Standard account | 4,000 | 5 seconds |
| Active feeder | 8,000 | 5 seconds |

Since 18 March 2026, OpenSky accepts **OAuth2 client credentials** only.
Username and password basic auth is rejected. Create an API client on the
OpenSky account page and set `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET`.
Leave both empty to stay anonymous. The monitor refreshes the bearer token
and retries once on HTTP 401. The secret is not logged.

Anonymous vectors do not change faster than every 10 seconds, so
`POLL_INTERVAL_SECONDS` must be **at least 10**. Faster polling spends a
credit on a duplicate snapshot. `OPENSKY_ALLOW_FAST_POLL=1` disables that
floor for tests.

This box costs 1 credit per request. Approximate spend:

| Interval | Credits per day | Fits |
| --- | --- | --- |
| 15s (default) | ~5,760 | about 1.7 hours anonymous, or a standard account for part of a day |
| 30s | ~2,880 | a standard account for 24 hours |
| 216s | 400 | anonymous, exactly the daily budget |
| 240s | 360 | anonymous for 24 hours, with a little headroom |

On startup the process logs the estimate and warns when it is over the
anonymous or standard-account budget. On HTTP 429 it sleeps for
`X-Rate-Limit-Retry-After-Seconds` (or `Retry-After`) and then continues.
`X-Rate-Limit-Remaining` is included in the poll log when OpenSky sends it.

Some cloud provider IP ranges get **HTTP 403** on anonymous requests. If
that happens, use OAuth credentials or run from a network OpenSky allows.

## Configuration

Copy `.env.example` to `.env`. Do not commit `.env` or a credentials file.
Empty values count as unset.

| Variable | Default | Role |
| --- | --- | --- |
| `AIRPORT_ICAO` | `ZGSZ` | Field to monitor. Anything else exits with an error in the MVP. |
| `OPENSKY_LAMIN` / `LAMAX` / `LOMIN` / `LOMAX` | box above | WGS84 bounds |
| `POLL_INTERVAL_SECONDS` | `15` | Pause between cycles. Minimum 10. |
| `DEBOUNCE_SAMPLES` | `2` | Consecutive polls required to commit an edge |
| `TRACK_TTL_SECONDS` | `900` | Drop an aircraft after this long with no sample |
| `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` | empty | OAuth2 client credentials. Both or neither. |
| `ALERT_WEBHOOK_URL` | empty | JSON POST target. Unset disables the webhook. |
| `ALERT_EMAIL_TO` | empty | Recipient. Unset keeps email in stub mode. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_USE_TLS` | port 587, TLS on | SMTP adapter |
| `LOG_LEVEL` | `INFO` | loguru level |
| `HTTP_TIMEOUT_SECONDS` | `20` | OpenSky and webhook HTTP timeout |
| `OPENSKY_ALLOW_FAST_POLL` | false | Test-only escape hatch for a sub-10s interval |

`--interval` overrides `POLL_INTERVAL_SECONDS` for one process.

## Layout

| Module | Role |
| --- | --- |
| `airport_monitor.ingest` | OpenSky HTTP, OAuth2 token refresh, state-vector parsing, credit estimate |
| `airport_monitor.state_machine` | Per-`icao24` debounce and takeoff/landing edges |
| `airport_monitor.alert` | Webhook POST and SMTP stub |
| `airport_monitor.airports` | ZGSZ catalog and the phase-2 extension point |
| `airport_monitor.loop` | Poll cycle |
| `airport_monitor.config` | Environment settings |

The loop already iterates a list of airports and keeps one tracker per ICAO
code. The catalog only contains ZGSZ.

## Phase 2

Domestic airports and user-selected subscriptions are not in this MVP.
The extension point is `src/airport_monitor/airports.py` (`AirportRegistry`
and the `TODO(phase-2)` note). Wire a subscription into `selected()` rather
than hard-coding another airport in the poll loop. Each extra box spends
another OpenSky credit every cycle.

Out of scope here: cameras, airline enrichment, Flightradar24, and other paid feeds.

## Tests

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

Detector tests feed fake `on_ground` sequences. HTTP tests use
`httpx.MockTransport`. Nothing in the test suite calls OpenSky.
