"""ZGSZ catalog coverage and the phase-2 selection hook."""

from __future__ import annotations

import pytest

from airport_monitor.airports import (
    ZGSZ,
    ZGSZ_ARP_LATITUDE,
    ZGSZ_ARP_LONGITUDE,
    ZGSZ_RUNWAY_THRESHOLDS,
    AirportRegistry,
    UnknownAirportError,
    resolve_airports,
)
from airport_monitor.models import BoundingBox


def test_default_box_covers_the_runways_with_margin() -> None:
    box = ZGSZ.box
    assert box.contains(ZGSZ_ARP_LATITUDE, ZGSZ_ARP_LONGITUDE)
    assert box.area_sq_deg == pytest.approx(0.0168)
    for name, latitude, longitude in ZGSZ_RUNWAY_THRESHOLDS:
        assert box.lamin + 0.02 <= latitude <= box.lamax - 0.02, name
        assert box.lomin + 0.02 <= longitude <= box.lomax - 0.02, name


def test_registry_is_zgsz_only() -> None:
    registry = AirportRegistry()
    assert registry.known() == ("ZGSZ",)
    assert registry.selected("zgsz")[0].name.startswith("Shenzhen Bao'an")


def test_unknown_airport_points_at_phase_2() -> None:
    with pytest.raises(UnknownAirportError, match="Phase 2") as caught:
        resolve_airports("ZBAA", ZGSZ.box)
    assert caught.value.icao == "ZBAA"


def test_env_box_overrides_the_catalog_box_for_the_single_field() -> None:
    override = BoundingBox(lamin=22.60, lamax=22.66, lomin=113.78, lomax=113.84)
    selected = resolve_airports("ZGSZ", override)
    assert selected[0].icao == "ZGSZ"
    assert selected[0].box == override
    assert ZGSZ.box != override
