"""Travel-time estimation with caching, daily budget and explicit origin resolution.

Origin order: explicit origin → the location the user shared in Telegram, while it is fresh at the departure
(`jarvis/reminders/origin.py`) — unless an event in between, not over yet, puts the user somewhere else first →
location of the previous event (if it ends shortly before) → configured DEFAULT_ORIGIN → none (no travel computed).

Providers that route only between coordinates (OpenRouteService) get them from `locate`: «lat,lng» passes through,
addresses are geocoded once and cached in `travel_estimates` (travel_mode GEOCODE) — no migration needed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.budget import consume
from jarvis.core.config import Settings
from jarvis.core.errors import BudgetExceededError
from jarvis.db.models import CalendarEvent, TravelEstimate
from jarvis.integrations.maps.routes import (
    NOT_CONFIGURED,
    Geocoder,
    GeocodeResult,
    GeoPoint,
    RouteResult,
    RoutesApi,
    RouteStatus,
    parse_coordinates,
)
from jarvis.reminders.origin import CurrentOrigin, load_origin

CACHE_TTL = timedelta(hours=12)  # planning estimate; the pre-departure refresh bypasses it
# Only answers are served from the cache: an outage, a rejected key or an exhausted quota is asked again next time
# (a key added later must work at once, not after 12 h of cached "unavailable").
CACHED_STATUSES = frozenset({RouteStatus.OK.value, RouteStatus.NOT_FOUND.value})
BUCKET_MINUTES = 15
PREVIOUS_EVENT_MAX_GAP = timedelta(hours=3)
# The traffic model's departure ≈ start − 60 min; it depends only on the occurrence (not on `now`), so periodic
# re-planning hits the cache. A shared location is the origin of an event whose departure falls in its fresh window.
DEPARTURE_LEAD = timedelta(minutes=60)
# Geocoding cache (rows of travel_estimates): addresses do not move; a miss is asked again after a day.
GEOCODE_MODE = "GEOCODE"
GEOCODE_BUCKET = datetime(2000, 1, 1, tzinfo=UTC)  # the column is NOT NULL; a geocode has no departure
GEOCODE_TTL = {RouteStatus.OK.value: timedelta(days=30), RouteStatus.NOT_FOUND.value: timedelta(days=1)}


@dataclass(frozen=True)
class TravelInfo:
    status: RouteStatus
    origin: str | None
    duration_seconds: int | None
    traffic_aware: bool
    computed_at: datetime | None
    note: str | None = None


def _bucket(value: datetime) -> datetime:
    minute = (value.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    return value.replace(minute=minute, second=0, microsecond=0)


def cache_key(origin: str, destination: str, bucket: datetime, mode: str) -> str:
    raw = f"{origin.strip().lower()}|{destination.strip().lower()}|{bucket.isoformat()}|{mode.upper()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def geocode_key(place: str, country: str) -> str:
    raw = f"geocode|{' '.join(place.split()).lower()}|{country.strip().upper()}"
    return hashlib.sha256(raw.encode()).hexdigest()


class TravelService:
    def __init__(self, routes: RoutesApi, settings: Settings, geocoder: Geocoder | None = None) -> None:
        self.routes = routes
        self.settings = settings
        self.geocoder = geocoder

    @property
    def origin_ttl(self) -> timedelta:
        return timedelta(minutes=self.settings.location_origin_ttl_minutes)

    async def shared_origin(self, session: AsyncSession, now: datetime) -> CurrentOrigin | None:
        """The location shared in Telegram if it is still fresh."""
        origin = await load_origin(session)
        return origin if origin is not None and origin.is_fresh(now, self.origin_ttl) else None

    async def resolve_origin(
        self, session: AsyncSession, event: CalendarEvent, explicit: str | None = None, *, now: datetime | None = None
    ) -> str | None:
        if explicit:
            return explicit
        previous = await self._previous_event(session, event)
        if now is not None and event.start_at is not None:
            shared = await self.shared_origin(session, now)
            if shared is not None and event.start_at - DEPARTURE_LEAD <= shared.fresh_until(self.origin_ttl):
                if previous is not None and previous.end_at is not None and previous.end_at > now:
                    return previous.location  # the user will be at that event first
                return shared.value
        if previous is not None:
            return previous.location
        return self.settings.default_origin

    async def _previous_event(self, session: AsyncSession, event: CalendarEvent) -> CalendarEvent | None:
        """An event with another address that ends shortly before this one (the user comes from there)."""
        if self.settings.use_previous_event_location_as_origin and event.start_at is not None:
            previous = await session.scalar(
                select(CalendarEvent)
                .where(
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.all_day.is_(False),
                    CalendarEvent.location.is_not(None),
                    CalendarEvent.end_at <= event.start_at,
                    CalendarEvent.end_at >= event.start_at - PREVIOUS_EVENT_MAX_GAP,
                    CalendarEvent.id != event.id,
                )
                .order_by(CalendarEvent.end_at.desc())
                .limit(1)
            )
            if previous is not None and previous.location and previous.location != event.location:
                return previous
        return None

    async def estimate(
        self,
        session: AsyncSession,
        *,
        origin: str,
        destination: str,
        departure: datetime,
        now: datetime,
        force_refresh: bool = False,
    ) -> TravelInfo:
        if not self.routes.configured:  # no key: nothing to ask, nothing to count or cache
            return TravelInfo(RouteStatus.UNAVAILABLE, origin, None, False, None, NOT_CONFIGURED)
        mode = self.settings.maps_travel_mode
        if mode.upper() not in self.routes.travel_modes:  # e.g. TRANSIT on OpenRouteService: never asked
            note = f"MAPS_TRAVEL_MODE={mode} is not supported by {self.routes.label}"
            return TravelInfo(RouteStatus.UNAVAILABLE, origin, None, False, None, note)
        bucket = _bucket(departure)
        key = cache_key(origin, destination, bucket, mode)
        cached = await session.scalar(select(TravelEstimate).where(TravelEstimate.cache_key == key))
        if (
            cached is not None
            and not force_refresh
            and cached.status in CACHED_STATUSES
            and now - cached.computed_at < CACHE_TTL
        ):
            return TravelInfo(
                RouteStatus(cached.status),
                origin,
                cached.duration_seconds,
                cached.traffic_aware,
                cached.computed_at,
                cached.error,
            )
        endpoints = [origin, destination]
        if self.routes.needs_coordinates:  # OpenRouteService: addresses → coordinates first (cached)
            endpoints = []
            for place in (origin, destination):
                found = await self.locate(session, place, now)
                if found.status != RouteStatus.OK or found.point is None:
                    if found.status == RouteStatus.NOT_FOUND:  # an answer: cached like a route that does not exist
                        missing = RouteResult(RouteStatus.NOT_FOUND, error=f"address not found: {found.error}")
                        await self._store(session, key, origin, destination, bucket, mode, missing, now)
                    return TravelInfo(found.status, origin, None, False, None, found.error)
                endpoints.append(found.point.value)
        try:
            await consume(session, self.routes.budget_key, now, self.settings.maps_max_requests_per_day)
        except BudgetExceededError as exc:
            if cached is not None and cached.status == RouteStatus.OK:
                return TravelInfo(
                    RouteStatus.OK,
                    origin,
                    cached.duration_seconds,
                    cached.traffic_aware,
                    cached.computed_at,
                    "stale (budget exhausted)",
                )
            return TravelInfo(RouteStatus.QUOTA, origin, None, False, None, str(exc))
        result: RouteResult = await self.routes.compute(endpoints[0], endpoints[1], departure, mode)
        await self._store(session, key, origin, destination, bucket, mode, result, now)
        return TravelInfo(result.status, origin, result.duration_seconds, result.traffic_aware, now, result.error)

    async def _store(
        self,
        session: AsyncSession,
        key: str,
        origin: str,
        destination: str,
        bucket: datetime,
        mode: str,
        result: RouteResult,
        now: datetime,
    ) -> None:
        values = {
            "cache_key": key,
            "origin": origin,
            "destination": destination,
            "departure_bucket": bucket,
            "travel_mode": mode,
            "status": result.status.value,
            "duration_seconds": result.duration_seconds,
            "static_duration_seconds": result.static_duration_seconds,
            "traffic_aware": result.traffic_aware,
            "provider": self.routes.provider,
            "error": result.error,
            "computed_at": now,
        }
        await _upsert(session, values)

    async def locate(self, session: AsyncSession, place: str, now: datetime) -> GeocodeResult:
        """A place → coordinates: «lat,lng» (a shared location) as is; an address through the geocoder, cached
        (OK for 30 days, «not found» for a day; outages and quota are not cached) and within its daily budget."""
        if (coords := parse_coordinates(place)) is not None:
            return GeocodeResult(RouteStatus.OK, GeoPoint(*coords))
        if self.geocoder is None:
            return GeocodeResult(RouteStatus.UNAVAILABLE, error="no geocoder (OPENROUTESERVICE_API_KEY not set)")
        key = geocode_key(place, self.settings.maps_geocode_country)
        cached = await session.scalar(select(TravelEstimate).where(TravelEstimate.cache_key == key))
        if cached is not None and now - cached.computed_at < GEOCODE_TTL.get(cached.status, timedelta(0)):
            if cached.status == RouteStatus.OK.value and (point := parse_coordinates(cached.destination)):
                return GeocodeResult(RouteStatus.OK, GeoPoint(*point))
            if cached.status == RouteStatus.NOT_FOUND.value:
                return GeocodeResult(RouteStatus.NOT_FOUND, error=cached.error)
        try:
            await consume(
                session, self.geocoder.geocode_budget_key, now, self.settings.maps_geocode_max_requests_per_day
            )
        except BudgetExceededError as exc:
            if (
                cached is not None
                and cached.status == RouteStatus.OK.value
                and (point := parse_coordinates(cached.destination))
            ):
                return GeocodeResult(RouteStatus.OK, GeoPoint(*point))  # stale but addresses do not move
            return GeocodeResult(RouteStatus.QUOTA, error=str(exc))
        result = await self.geocoder.geocode(place)
        if result.status in (RouteStatus.OK, RouteStatus.NOT_FOUND):
            await _upsert(
                session,
                {
                    "cache_key": key,
                    "origin": place,
                    "destination": result.point.value if result.point is not None else "",
                    "departure_bucket": GEOCODE_BUCKET,
                    "travel_mode": GEOCODE_MODE,
                    "status": result.status.value,
                    "duration_seconds": None,
                    "static_duration_seconds": None,
                    "traffic_aware": False,
                    "provider": self.geocoder.provider,
                    "error": result.error,
                    "computed_at": now,
                },
            )
        return result


async def _upsert(session: AsyncSession, values: dict[str, object]) -> None:
    stmt = insert(TravelEstimate).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TravelEstimate.cache_key],
            set_={k: stmt.excluded[k] for k in values if k != "cache_key"},
        )
    )
