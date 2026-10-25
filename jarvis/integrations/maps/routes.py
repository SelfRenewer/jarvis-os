"""Google Maps Routes API (computeRoutes) adapter.

Never invents travel time: every failure mode returns a typed status and no duration.

Billing (checked 2026-10-14, https://developers.google.com/maps/billing-and-pricing/sku-details): one SKU per
request — Compute Routes *Essentials* (basic), *Pro* (`routingPreference` TRAFFIC_AWARE / TRAFFIC_AWARE_OPTIMAL),
*Enterprise* (two-wheeler routing, tolls). The field mask below asks only for basic fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Protocol

import httpx

from jarvis.core.errors import PermanentError, RateLimitedError, TransientError
from jarvis.core.http import ResilientHttp

URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
GOOGLE_MODES = frozenset({"DRIVE", "WALK", "BICYCLE", "TRANSIT", "TWO_WHEELER"})
TRAFFIC_MODES = {"DRIVE", "TWO_WHEELER"}  # `routingPreference` is valid only for these (otherwise the request fails)
# `departureTime` must be in the future when the server receives it: a departure closer than this is sent as
# "now" (field omitted) instead of risking a 400 that would look like an unknown address.
DEPARTURE_MARGIN = timedelta(minutes=1)
NOT_CONFIGURED = "maps not configured (set OPENROUTESERVICE_API_KEY or GOOGLE_MAPS_API_KEY)"
_COORDINATES = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coordinates(value: str) -> tuple[float, float] | None:
    """«41.74,44.82» → (lat, lng); anything else (an address) → None."""
    m = _COORDINATES.match(value)
    if m is None:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def format_coordinates(latitude: float, longitude: float) -> str:
    return f"{latitude:.5f},{longitude:.5f}"


def waypoint(value: str) -> dict[str, Any]:
    """Coordinates (a location shared in Telegram) → `location.latLng`; everything else → `address`."""
    if (point := parse_coordinates(value)) is not None:
        return {"location": {"latLng": {"latitude": point[0], "longitude": point[1]}}}
    return {"address": value}


def billing_sku(travel_mode: str, traffic_aware: bool) -> str:
    """Which Compute Routes SKU a request of this adapter is billed under."""
    mode = travel_mode.upper()
    if mode == "TWO_WHEELER":
        return "Enterprise"
    if mode in TRAFFIC_MODES and traffic_aware:
        return "Pro"
    return "Essentials"


class RouteStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"  # address could not be geocoded / no route
    UNAVAILABLE = "unavailable"  # API down, not configured
    QUOTA = "quota"  # rate limit / daily budget


@dataclass(frozen=True)
class RouteResult:
    status: RouteStatus
    duration_seconds: int | None = None
    static_duration_seconds: int | None = None
    distance_meters: int | None = None
    traffic_aware: bool = False
    error: str | None = None


def _seconds(value: str | None) -> int | None:
    if not value:
        return None
    return round(float(value.rstrip("s")))


class RoutesApi(Protocol):
    configured: ClassVar[bool]  # False: no key — callers skip the request (no budget, no cached failure)
    provider: ClassVar[str]  # stored with cached estimates
    label: ClassVar[str]  # shown to the user («… не нашёл дорогу»)
    budget_key: ClassVar[str]  # the daily request counter (`usage_counters.provider`)
    # True: the provider routes only between coordinates — addresses are geocoded (and cached) by TravelService.
    needs_coordinates: ClassVar[bool]
    travel_modes: ClassVar[frozenset[str]]  # MAPS_TRAVEL_MODE values it can route

    async def compute(self, origin: str, destination: str, departure: datetime, travel_mode: str) -> RouteResult: ...


class HttpRoutesApi:
    configured: ClassVar[bool] = True
    provider: ClassVar[str] = "google_routes"
    label: ClassVar[str] = "Google Maps"
    budget_key: ClassVar[str] = "google_maps"
    needs_coordinates: ClassVar[bool] = False  # Google geocodes `address` waypoints itself
    travel_modes: ClassVar[frozenset[str]] = GOOGLE_MODES

    def __init__(
        self, api_key: str, *, traffic_aware: bool = True, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.traffic_aware = traffic_aware
        self._http = ResilientHttp(
            "google-routes",
            timeout=10.0,
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def compute(self, origin: str, destination: str, departure: datetime, travel_mode: str) -> RouteResult:
        mode = travel_mode.upper()
        body: dict[str, object] = {
            "origin": waypoint(origin),
            "destination": waypoint(destination),
            "travelMode": mode,
            "languageCode": "ru",
            "units": "METRIC",
        }
        traffic = mode in TRAFFIC_MODES and self.traffic_aware
        if traffic:
            body["routingPreference"] = "TRAFFIC_AWARE"  # Pro SKU; omitted = TRAFFIC_UNAWARE (Essentials)
        # departureTime matters only for live traffic and transit schedules ("not recommended" otherwise).
        if (traffic or mode == "TRANSIT") and departure > datetime.now(UTC) + DEPARTURE_MARGIN:
            body["departureTime"] = departure.astimezone(UTC).isoformat().replace("+00:00", "Z")
        try:
            data = (await self._http.post(URL, json=body, retry_non_idempotent=True)).json()
        except RateLimitedError as exc:
            return RouteResult(RouteStatus.QUOTA, error=str(exc))
        except TransientError as exc:
            return RouteResult(RouteStatus.UNAVAILABLE, error=str(exc))
        except PermanentError as exc:
            text = str(exc)
            if "API key" in text or "API_KEY" in text:  # Google answers an invalid key with HTTP 400
                return RouteResult(RouteStatus.UNAVAILABLE, error="Google rejected the API key: " + text[:160])
            if "HTTP 400" in text or "NOT_FOUND" in text:
                return RouteResult(RouteStatus.NOT_FOUND, error=text[:200])
            return RouteResult(RouteStatus.UNAVAILABLE, error=text[:200])
        routes = data.get("routes") or []
        if not routes:
            return RouteResult(RouteStatus.NOT_FOUND, error="no route returned")
        route = routes[0]
        duration = _seconds(route.get("duration"))
        if duration is None:
            return RouteResult(RouteStatus.NOT_FOUND, error="route without duration")
        return RouteResult(
            RouteStatus.OK,
            duration_seconds=duration,
            static_duration_seconds=_seconds(route.get("staticDuration")),
            distance_meters=route.get("distanceMeters"),
            traffic_aware=traffic,
        )


@dataclass
class FakeRoutesApi:
    configured: ClassVar[bool] = True
    provider: ClassVar[str] = "fake"
    label: ClassVar[str] = "Google Maps"
    budget_key: ClassVar[str] = "google_maps"
    needs_coordinates: ClassVar[bool] = False
    travel_modes: ClassVar[frozenset[str]] = GOOGLE_MODES
    durations: dict[tuple[str, str], int] = field(default_factory=dict)
    default_status: RouteStatus = RouteStatus.NOT_FOUND
    calls: list[tuple[str, str, datetime, str]] = field(default_factory=list)

    async def compute(self, origin: str, destination: str, departure: datetime, travel_mode: str) -> RouteResult:
        self.calls.append((origin, destination, departure, travel_mode))
        seconds = self.durations.get((origin, destination))
        if seconds is None:
            return RouteResult(self.default_status, error="fake: unknown route")
        return RouteResult(
            RouteStatus.OK,
            duration_seconds=seconds,
            static_duration_seconds=seconds,
            traffic_aware=travel_mode.upper() in TRAFFIC_MODES,
        )


class UnavailableRoutesApi:
    configured: ClassVar[bool] = False
    provider: ClassVar[str] = "none"
    label: ClassVar[str] = "Google Maps"
    budget_key: ClassVar[str] = "maps"
    needs_coordinates: ClassVar[bool] = False
    travel_modes: ClassVar[frozenset[str]] = GOOGLE_MODES

    async def compute(self, origin: str, destination: str, departure: datetime, travel_mode: str) -> RouteResult:
        return RouteResult(RouteStatus.UNAVAILABLE, error=NOT_CONFIGURED)


# --- geocoding (addresses → coordinates) ------------------------------------------------------------------------


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    label: str | None = None  # the provider's name for the match (never logged: it may be a home address)

    @property
    def value(self) -> str:
        return format_coordinates(self.latitude, self.longitude)


@dataclass(frozen=True)
class GeocodeResult:
    status: RouteStatus  # OK / NOT_FOUND (no precise match) / UNAVAILABLE / QUOTA
    point: GeoPoint | None = None
    error: str | None = None


class Geocoder(Protocol):
    provider: ClassVar[str]
    geocode_budget_key: ClassVar[str]  # its own daily counter (not the routing one, even on the same provider)

    async def geocode(self, text: str) -> GeocodeResult: ...


@dataclass
class FakeGeocoder:
    provider: ClassVar[str] = "fake"
    geocode_budget_key: ClassVar[str] = "geocode"
    points: dict[str, GeoPoint] = field(default_factory=dict)
    default_status: RouteStatus = RouteStatus.NOT_FOUND
    calls: list[str] = field(default_factory=list)

    async def geocode(self, text: str) -> GeocodeResult:
        if (coords := parse_coordinates(text)) is not None:
            return GeocodeResult(RouteStatus.OK, GeoPoint(*coords))
        self.calls.append(text)
        point = self.points.get(text)
        if point is None:
            return GeocodeResult(self.default_status, error="fake: unknown address")
        return GeocodeResult(RouteStatus.OK, point)


__all__ = [
    "FakeGeocoder",
    "FakeRoutesApi",
    "GeoPoint",
    "GeocodeResult",
    "Geocoder",
    "HttpRoutesApi",
    "PermanentError",
    "RouteResult",
    "RouteStatus",
    "RoutesApi",
    "UnavailableRoutesApi",
    "billing_sku",
    "format_coordinates",
    "parse_coordinates",
    "waypoint",
]
