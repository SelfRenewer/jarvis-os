"""OpenRouteService adapter: travel time on OpenStreetMap data (no live traffic) + Pelias geocoding.

Free Standard plan, no bank card. Checked 2026-10-14:

* Quotas (the Standard policy shown on https://account.heigit.org/info/plans, formerly openrouteservice.org/plans):
  directions 2,000/day and 40/min; geocode search 1,000/day and 100/min.
* Directions: ``POST https://api.openrouteservice.org/v2/directions/{profile}/json`` with the key in the
  ``Authorization`` header and ``{"coordinates": [[lon, lat], [lon, lat]]}``; the answer is
  ``routes[0].summary.duration`` (s) / ``.distance`` (m)
  (https://giscience.github.io/openrouteservice/api-reference/endpoints/directions/requests-and-return-types).
  Zero values are left out of the summary. A point far from any road or no route → HTTP 404
  (PointNotFoundException / RouteNotFoundException → StatusCode.NOT_FOUND, internal codes 2010 / 2009).
* Geocoding (Pelias): ``GET https://api.openrouteservice.org/geocode/search?text=…`` with ``boundary.country``,
  ``focus.point.lat`` / ``focus.point.lon``, ``size``; a GeoJSON FeatureCollection whose
  ``features[].geometry.coordinates`` are ``[lon, lat]`` (https://github.com/pelias/documentation, response.md),
  ``properties.layer`` / ``confidence`` / ``label``. The key goes in the header here too, like the official
  client (openrouteservice-py) does — never in the URL.
* Without a key the gateway answers 401 «Authorization field missing», with a wrong key 403 «Access to this API has
  been disallowed» (both checked live without a key); over quota → 429.

* Pelias does not know many streets and buildings in smaller cities (found live: a street address → only the city),
  while OpenStreetMap's own geocoder Nominatim finds the house. So «no precise match» from Pelias is retried with
  Nominatim: ``GET https://nominatim.openstreetmap.org/search?format=jsonv2`` — free, no key; its usage policy asks
  for an identifying User-Agent, at most 1 request/s and cached results
  (https://operations.osmfoundation.org/policies/nominatim/). Only street-level or finer answers (place_rank ≥ 26).

Never invents travel time: every failure is a typed status without a duration. Only coarse matches (a whole city or
region) are rejected as «not found»: a route to a city centroid would look right and be wrong.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from typing import Any, ClassVar

import httpx

from jarvis.core.errors import AuthError, NotFoundError, PermanentError, RateLimitedError, TransientError
from jarvis.core.http import ResilientHttp
from jarvis.integrations.maps.routes import GeocodeResult, GeoPoint, RouteResult, RouteStatus, parse_coordinates

BASE_URL = "https://api.openrouteservice.org"
DIRECTIONS_PATH = "/v2/directions/{profile}/json"
GEOCODE_PATH = "/geocode/search"
DAILY_DIRECTIONS_QUOTA = 2000  # Standard plan (free)
DAILY_GEOCODE_QUOTA = 1000
PROFILES = {"DRIVE": "driving-car", "WALK": "foot-walking", "BICYCLE": "cycling-regular"}  # TRANSIT: none
# Pelias layers precise enough to route to; locality / region / country and the like are a city-sized guess.
PRECISE_LAYERS = frozenset({"venue", "address", "street"})
MIN_CONFIDENCE = 0.5
GEOCODE_CANDIDATES = 5
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_USER_AGENT = "JARVIS-personal-assistant/1.0 (single user, travel time; results cached)"
NOMINATIM_MIN_RANK = 26  # 26–27 street, 30 house/building; a city is 16


def ors_profile(travel_mode: str) -> str | None:
    return PROFILES.get(travel_mode.upper())


def _error(text: str) -> str:
    return text[:200]


def _failure(exc: Exception) -> tuple[RouteStatus, str]:
    """A provider error → (status, error). Order matters: the subclasses come first."""
    if isinstance(exc, RateLimitedError):
        return RouteStatus.QUOTA, _error(str(exc))
    if isinstance(exc, TransientError):
        return RouteStatus.UNAVAILABLE, _error(str(exc))
    if isinstance(exc, AuthError):
        return RouteStatus.UNAVAILABLE, _error(f"OpenRouteService rejected the API key: {exc}")
    if isinstance(exc, NotFoundError):
        return RouteStatus.NOT_FOUND, _error(str(exc))
    text = str(exc)
    if "HTTP 400" in text:  # invalid / out-of-range points (e.g. a route over the distance limit)
        return RouteStatus.NOT_FOUND, _error(text)
    return RouteStatus.UNAVAILABLE, _error(text)


def _point(feature: dict[str, Any]) -> GeoPoint | None:
    """A precise enough Pelias feature → a point; a coarse or malformed one → None."""
    props = feature.get("properties") or {}
    if props.get("layer") not in PRECISE_LAYERS:
        return None
    confidence = props.get("confidence")
    if isinstance(confidence, int | float) and confidence < MIN_CONFIDENCE:
        return None
    coordinates = (feature.get("geometry") or {}).get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])  # GeoJSON order: [lon, lat]
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    label = props.get("label")
    return GeoPoint(latitude, longitude, label if isinstance(label, str) else None)


def _nominatim_point(item: dict[str, Any]) -> GeoPoint | None:
    try:
        rank = int(item.get("place_rank") or 0)
        latitude, longitude = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if rank < NOMINATIM_MIN_RANK or not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    label = item.get("display_name")
    return GeoPoint(latitude, longitude, label if isinstance(label, str) else None)


class OpenRouteServiceRoutesApi:
    configured: ClassVar[bool] = True
    provider: ClassVar[str] = "openrouteservice"
    label: ClassVar[str] = "OpenRouteService"
    budget_key: ClassVar[str] = "openrouteservice"
    geocode_budget_key: ClassVar[str] = "openrouteservice_geocode"
    needs_coordinates: ClassVar[bool] = True
    travel_modes: ClassVar[frozenset[str]] = frozenset(PROFILES)
    nominatim_interval: ClassVar[float] = 1.1  # seconds between Nominatim requests (policy: max 1/s)
    _nominatim_lock: ClassVar[asyncio.Lock | None] = None
    _nominatim_last: ClassVar[float] = 0.0

    def __init__(
        self,
        api_key: str,
        *,
        focus: tuple[float, float] | None = None,
        country: str | None = None,
        nominatim: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.focus = focus  # (lat, lng): results near the user's city rank first
        self.country = (country or "").strip().upper() or None  # ISO 3166 alpha-2/3, e.g. GE
        self._http = ResilientHttp(
            "openrouteservice",
            base_url=BASE_URL,
            timeout=10.0,
            headers={"Authorization": api_key, "Accept": "application/json, application/geo+json"},
            transport=transport,
        )
        self._nominatim = (
            ResilientHttp(
                "nominatim",
                base_url=NOMINATIM_URL,
                timeout=10.0,
                headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept": "application/json"},
                transport=transport,
            )
            if nominatim
            else None
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._nominatim is not None:
            await self._nominatim.aclose()

    async def _nominatim_get(self, path: str, params: dict[str, str]) -> Any:
        """One Nominatim request under its usage policy (≤ 1 request/s across this process); None on any failure."""
        if self._nominatim is None:
            return None
        cls = type(self)
        if cls._nominatim_lock is None:
            cls._nominatim_lock = asyncio.Lock()
        async with cls._nominatim_lock:
            wait = cls._nominatim_last + cls.nominatim_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return (await self._nominatim.get(path, params=params)).json()
            except (TransientError, PermanentError, ValueError):
                return None
            finally:
                cls._nominatim_last = time.monotonic()

    async def _nominatim_search(self, query: str) -> GeoPoint | None:
        """OpenStreetMap's geocoder for what Pelias only resolves to the city (policy: ≤ 1 request/s, cached)."""
        params = {"q": query, "format": "jsonv2", "limit": "3"}
        if self.country and len(self.country) == 2:
            params["countrycodes"] = self.country.lower()
        data = await self._nominatim_get("/search", params)
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and (point := _nominatim_point(item)) is not None:
                return point
        return None

    async def reverse(self, latitude: float, longitude: float) -> str | None:
        """Coordinates → «улица, дом · район» for «📍 Вы здесь» (Nominatim reverse; never logged — it may be home)."""
        params = {
            "lat": f"{latitude:.6f}",
            "lon": f"{longitude:.6f}",
            "format": "jsonv2",
            "zoom": "18",
            "accept-language": "ru",
        }
        data = await self._nominatim_get("/reverse", params)
        address = data.get("address") if isinstance(data, dict) else None
        if not isinstance(address, dict):
            return None
        road = next((address[k] for k in ("road", "pedestrian", "footway", "square") if address.get(k)), None)
        street = ", ".join(str(x) for x in (road, address.get("house_number")) if x)
        area = next(
            (address[k] for k in ("neighbourhood", "quarter", "suburb", "city_district") if address.get(k)), None
        )
        label = " · ".join(str(x) for x in (street, area) if x)
        return label[:120] or None

    async def geocode(self, text: str) -> GeocodeResult:
        """An address → the best precise match; «lat,lng» passes through without a request."""
        if (coords := parse_coordinates(text)) is not None:
            return GeocodeResult(RouteStatus.OK, GeoPoint(*coords))
        query = " ".join(text.split())
        if not query:
            return GeocodeResult(RouteStatus.NOT_FOUND, error="empty address")
        params: dict[str, str] = {"text": query, "size": str(GEOCODE_CANDIDATES)}
        if self.country:
            params["boundary.country"] = self.country
        if self.focus is not None:
            params["focus.point.lat"] = f"{self.focus[0]:.5f}"
            params["focus.point.lon"] = f"{self.focus[1]:.5f}"
        try:
            data = (await self._http.get(GEOCODE_PATH, params=params)).json()
        except (TransientError, PermanentError) as exc:
            status, error = _failure(exc)
            return GeocodeResult(status, error=error)
        except ValueError:
            return GeocodeResult(RouteStatus.UNAVAILABLE, error="openrouteservice: geocode answer is not JSON")
        features = data.get("features") if isinstance(data, dict) else None
        for feature in features or []:
            if isinstance(feature, dict) and (point := _point(feature)) is not None:
                return GeocodeResult(RouteStatus.OK, point)
        if (point := await self._nominatim_search(query)) is not None:  # Pelias knew only the city
            return GeocodeResult(RouteStatus.OK, point)
        # «Blue Door Cafe, 12 Rustaveli Ave, Tbilisi»: a venue name in front confuses Nominatim — try without it.
        parts = [part.strip() for part in query.split(",") if part.strip()]
        if len(parts) >= 3 and (point := await self._nominatim_search(", ".join(parts[1:]))) is not None:
            return GeocodeResult(RouteStatus.OK, point)
        return GeocodeResult(RouteStatus.NOT_FOUND, error="no precise match (a place, house or street) for the address")

    async def compute(self, origin: str, destination: str, departure: datetime, travel_mode: str) -> RouteResult:
        """`departure` is unused: OpenRouteService has no live traffic (the same answer at any time)."""
        mode = travel_mode.upper()
        profile = ors_profile(mode)
        if profile is None:
            return RouteResult(
                RouteStatus.UNAVAILABLE, error=f"travel mode {mode} is not supported by OpenRouteService"
            )
        points: list[GeoPoint] = []
        for place in (origin, destination):
            found = await self.geocode(place)
            if found.status != RouteStatus.OK or found.point is None:
                return RouteResult(found.status, error=found.error)
            points.append(found.point)
        body = {
            "coordinates": [[p.longitude, p.latitude] for p in points],  # [lon, lat]!
            "instructions": False,
            "geometry": False,
            "units": "m",
        }
        try:
            response = await self._http.post(
                DIRECTIONS_PATH.format(profile=profile), json=body, retry_non_idempotent=True
            )
            data = response.json()
        except (TransientError, PermanentError) as exc:
            status, error = _failure(exc)
            return RouteResult(status, error=error)
        except ValueError:
            return RouteResult(RouteStatus.UNAVAILABLE, error="openrouteservice: directions answer is not JSON")
        routes = data.get("routes") if isinstance(data, dict) else None
        if not routes or not isinstance(routes[0], dict):
            return RouteResult(RouteStatus.NOT_FOUND, error="no route returned")
        summary = routes[0].get("summary")
        if not isinstance(summary, dict):
            return RouteResult(RouteStatus.NOT_FOUND, error="route without summary")
        try:
            # Zero values are omitted by the API: a route between the same points has an empty summary.
            duration = round(float(summary.get("duration", 0)))
            distance = round(float(summary.get("distance", 0)))
        except (TypeError, ValueError):
            return RouteResult(RouteStatus.NOT_FOUND, error="route with an unreadable summary")
        return RouteResult(
            RouteStatus.OK,
            duration_seconds=duration,
            static_duration_seconds=duration,
            distance_meters=distance,
            traffic_aware=False,
        )


__all__ = [
    "DAILY_DIRECTIONS_QUOTA",
    "DAILY_GEOCODE_QUOTA",
    "OpenRouteServiceRoutesApi",
    "ors_profile",
]
