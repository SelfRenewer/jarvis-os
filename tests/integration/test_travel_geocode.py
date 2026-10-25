"""TravelService with a provider that routes only between coordinates (OpenRouteService): geocoding cache, budgets.

Fakes only: an ORS-shaped FakeRoutesApi (needs_coordinates) + FakeGeocoder; caches live in travel_estimates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from jarvis.db.models import TravelEstimate, UsageCounter
from jarvis.integrations.maps.routes import FakeGeocoder, FakeRoutesApi, GeoPoint, RouteStatus, format_coordinates
from jarvis.reminders.travel import GEOCODE_MODE, TravelService
from tests.conftest import make_settings

pytestmark = pytest.mark.db
TZ = ZoneInfo("Asia/Tbilisi")
HOME_ADDR = "Rustaveli Ave 1, Tbilisi"  # DEFAULT_ORIGIN in the test settings
DEST = "Tbilisi Boulevard 5"
HOME = GeoPoint(41.7443, 44.8304)
DEST_POINT = GeoPoint(41.7483, 44.8204)


class CoordinateRoutes(FakeRoutesApi):
    """Like OpenRouteService: only coordinates, no traffic, its own budget counter."""

    provider: ClassVar[str] = "openrouteservice"
    label: ClassVar[str] = "OpenRouteService"
    budget_key: ClassVar[str] = "openrouteservice"
    needs_coordinates: ClassVar[bool] = True
    travel_modes: ClassVar[frozenset[str]] = frozenset({"DRIVE", "WALK", "BICYCLE"})


def local(h: int, m: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def setup(c: Any, **overrides: object) -> tuple[TravelService, CoordinateRoutes, FakeGeocoder]:
    routes = CoordinateRoutes()
    routes.durations[(HOME.value, DEST_POINT.value)] = 900
    geocoder = FakeGeocoder(points={HOME_ADDR: HOME, DEST: DEST_POINT})
    settings = make_settings(**overrides) if overrides else c.settings
    return TravelService(routes, settings, geocoder), routes, geocoder


async def estimate(c: Any, travel: TravelService, origin: str = HOME_ADDR, departure: datetime | None = None) -> Any:
    async with c.db.session() as s:
        return await travel.estimate(
            s, origin=origin, destination=DEST, departure=departure or local(15), now=c.clock.now()
        )


async def counters(c: Any) -> dict[str, int]:
    async with c.db.session() as s:
        return {row.provider: row.count for row in (await s.scalars(select(UsageCounter))).all()}


async def test_addresses_are_geocoded_once_and_routed_between_coordinates(container: Any) -> None:
    c = container
    travel, routes, geocoder = setup(c)
    first = await estimate(c, travel)
    assert (first.status, first.duration_seconds, first.origin) == (RouteStatus.OK, 900, HOME_ADDR)
    assert routes.calls[0][:2] == (HOME.value, DEST_POINT.value)  # the provider got coordinates
    assert sorted(geocoder.calls) == sorted([HOME_ADDR, DEST])
    # Another departure (a new travel cache key): the addresses come from the geocode cache.
    second = await estimate(c, travel, departure=local(18))
    assert second.duration_seconds == 900
    assert len(routes.calls) == 2 and len(geocoder.calls) == 2
    async with c.db.session() as s:
        rows = (await s.scalars(select(TravelEstimate))).all()
    geocoded = {r.origin: r.destination for r in rows if r.travel_mode == GEOCODE_MODE}
    assert geocoded == {HOME_ADDR: HOME.value, DEST: DEST_POINT.value}
    assert {r.provider for r in rows if r.travel_mode != GEOCODE_MODE} == {"openrouteservice"}
    assert await counters(c) == {"openrouteservice": 2, "geocode": 2}


async def test_shared_location_needs_no_geocoding(container: Any) -> None:
    c = container
    travel, _routes, geocoder = setup(c)
    info = await estimate(c, travel, origin=HOME.value)
    assert info.status == RouteStatus.OK
    assert geocoder.calls == [DEST]  # only the destination address


async def test_unknown_address_is_cached_as_not_found(container: Any) -> None:
    c = container
    travel, routes, geocoder = setup(c)
    geocoder.points.pop(DEST)
    first = await estimate(c, travel)
    assert first.status == RouteStatus.NOT_FOUND and first.duration_seconds is None
    second = await estimate(c, travel)
    assert second.status == RouteStatus.NOT_FOUND
    assert routes.calls == []  # never routed to a guessed point
    assert geocoder.calls.count(DEST) == 1  # the miss is cached
    c.clock.advance(timedelta(days=1, minutes=1))  # a miss is asked again after a day…
    geocoder.points[DEST] = DEST_POINT  # …e.g. after the address was fixed on the map
    third = await estimate(c, travel, departure=local(15, day=24))
    assert (third.status, third.duration_seconds) == (RouteStatus.OK, 900)


@pytest.mark.parametrize("status", [RouteStatus.UNAVAILABLE, RouteStatus.QUOTA])
async def test_geocoder_outage_is_not_cached_and_spends_no_routing(container: Any, status: RouteStatus) -> None:
    c = container
    travel, routes, geocoder = setup(c)
    geocoder.points.clear()
    geocoder.default_status = status
    info = await estimate(c, travel)
    assert info.status == status
    assert routes.calls == []
    async with c.db.session() as s:
        assert (await s.scalars(select(TravelEstimate))).all() == []
    assert "openrouteservice" not in await counters(c)
    geocoder.points.update({HOME_ADDR: HOME, DEST: DEST_POINT})
    assert (await estimate(c, travel)).status == RouteStatus.OK  # the next try works at once


async def test_geocode_budget_is_capped(container: Any) -> None:
    c = container
    travel, routes, geocoder = setup(c, maps_geocode_max_requests_per_day=1)
    info = await estimate(c, travel)
    assert info.status == RouteStatus.QUOTA  # home geocoded, the destination hit the cap
    assert geocoder.calls == [HOME_ADDR] and routes.calls == []


async def test_unsupported_mode_is_never_asked(container: Any) -> None:
    c = container
    travel, routes, geocoder = setup(c, maps_travel_mode="TRANSIT")
    info = await estimate(c, travel)
    assert info.status == RouteStatus.UNAVAILABLE
    assert "TRANSIT" in (info.note or "") and "OpenRouteService" in (info.note or "")
    assert routes.calls == [] and geocoder.calls == []
    assert await counters(c) == {}


async def test_locate_without_geocoder(container: Any) -> None:
    c = container
    travel = TravelService(FakeRoutesApi(), c.settings)  # no OPENROUTESERVICE_API_KEY
    async with c.db.session() as s:
        coords = await travel.locate(s, format_coordinates(41.7383, 44.8204), c.clock.now())
        address = await travel.locate(s, DEST, c.clock.now())
    assert coords.status == RouteStatus.OK and coords.point is not None and coords.point.latitude == 41.7383
    assert address.status == RouteStatus.UNAVAILABLE and address.point is None
