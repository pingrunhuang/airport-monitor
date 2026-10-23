"""Airport catalog.

MVP ships a single field: Shenzhen Bao'an, ICAO ZGSZ.

---------------------------------------------------------------------------
TODO(phase-2): domestic airport list and user-selected alerts.

1. Extend ``AirportRegistry`` with more mainland airports (ICAO, name, box).
2. Replace the single ``AIRPORT_ICAO`` setting with a subscription, for
   example ``ALERT_AIRPORTS=ZGSZ,ZGGG,ZUUU``.
3. ``run_monitor`` already polls every airport it is given and keeps one
   ``GroundStateTracker`` per ICAO code. Each extra box costs another
   OpenSky credit per cycle, so budget the interval before enabling more
   than a handful of fields.

Do not add cameras or airline enrichment in this module.
---------------------------------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from airport_monitor.models import BoundingBox

# ARP from the CAAC AIP: N22°38.3' E113°48.7', center of runway 15/33.
ZGSZ_ARP_LATITUDE = 22 + 38.3 / 60
ZGSZ_ARP_LONGITUDE = 113 + 48.7 / 60

# Approximate runway-threshold positions (WGS84 degrees). Used only to check
# that the monitor box covers the field. Not a navigation source.
ZGSZ_RUNWAY_THRESHOLDS: tuple[tuple[str, float, float], ...] = (
    ("15", 22.6530, 113.8030),
    ("33", 22.6256, 113.8180),
    ("16L", 22.6545, 113.7849),
    ("34R", 22.6239, 113.8016),
    ("16R", 22.6591, 113.7763),
    ("34L", 22.6301, 113.7922),
)

# The brief suggested lat 22.55–22.70, lon 113.75–113.95. That box contains
# the airport, but it runs about 14 km east of the eastern threshold and
# leaves only ~2.7 km west of runway 16R. The box below is centered on the
# three runway pairs, with roughly 4–6 km of margin, and still costs 1 credit
# (0.12° × 0.14° = 0.0168 sq deg). Override with OPENSKY_LA* / OPENSKY_LO*.
ZGSZ_BOX = BoundingBox(lamin=22.58, lamax=22.70, lomin=113.74, lomax=113.88)


@dataclass(frozen=True)
class Airport:
    icao: str
    name: str
    city: str
    box: BoundingBox


ZGSZ = Airport(
    icao="ZGSZ",
    name="Shenzhen Bao'an International Airport",
    city="Shenzhen",
    box=ZGSZ_BOX,
)


class UnknownAirportError(Exception):
    """The requested ICAO code is not in the MVP catalog."""

    def __init__(self, icao: str, known: Sequence[str]) -> None:
        self.icao = icao
        self.known = tuple(known)
        known_text = ", ".join(self.known) if self.known else "(none)"
        super().__init__(
            f"Airport {icao} is not in the MVP registry (known: {known_text}). "
            "Phase 2 adds a domestic airport list; register the field in "
            "airport_monitor.airports and select it from the user subscription."
        )


class AirportRegistry:
    """Catalog of fields the monitor knows how to poll.

    ``selected`` is the MVP entry point (one ICAO code from settings).
    Phase 2 should filter this catalog by the user's subscription instead
    of growing a second code path in the poll loop.
    """

    def __init__(self, airports: tuple[Airport, ...] = (ZGSZ,)) -> None:
        self._airports = airports

    def known(self) -> tuple[str, ...]:
        return tuple(airport.icao for airport in self._airports)

    def selected(self, icao: str) -> tuple[Airport, ...]:
        code = icao.strip().upper()
        matches = tuple(airport for airport in self._airports if airport.icao == code)
        if not matches:
            raise UnknownAirportError(code, self.known())
        return matches


def resolve_airports(
    icao: str,
    box: BoundingBox,
    registry: AirportRegistry | None = None,
) -> tuple[Airport, ...]:
    """Return the airports to poll, applying the MVP bounding-box override.

    ``OPENSKY_LAMIN`` and the other three settings replace the catalog box
    while a single airport is selected. Phase 2, with several subscribed
    fields, should keep each catalog box and stop applying one global box.
    """

    selected = (registry or AirportRegistry()).selected(icao)
    if len(selected) == 1:
        return (replace(selected[0], box=box),)
    return selected
