"""The user's current starting point for travel time: a location shared in Telegram.

* A location (📎 → Геопозиция, or a reply-keyboard button with `request_location`) is used for
  `LOCATION_ORIGIN_TTL_MINUTES` after it was sent.
* A live location while Telegram keeps updating it: its sharing window is open and the last update is at most
  the same TTL old. Updates arrive as `edited_message`; an update without `live_period` means sharing stopped.
* «Я дома» forgets it → DEFAULT_ORIGIN again.

Stored in `user_preferences` (no migration). Coordinates are private: never logged and never shown back to the user.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.clock import format_local
from jarvis.db.models import UserPreference
from jarvis.integrations.maps.routes import format_coordinates

ORIGIN_PREF_KEY = "current_origin"
# Smaller moves keep the stored point, so GPS jitter and a walk around the block keep hitting the travel cache.
MIN_MOVE_METERS = 300.0
_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class CurrentOrigin:
    latitude: float
    longitude: float
    updated_at: datetime  # when the point was sent / last updated (Telegram's time, never in the future)
    live_until: datetime | None = None  # live location: the end of its sharing window
    message_id: int | None = None  # the Telegram message (live updates of other messages are ignored)

    @property
    def live(self) -> bool:
        return self.live_until is not None

    @property
    def value(self) -> str:
        """The origin passed to the Routes API (`lat,lng` → a latLng waypoint)."""
        return format_coordinates(self.latitude, self.longitude)

    def fresh_until(self, ttl: timedelta) -> datetime:
        until = self.updated_at + ttl
        return min(until, self.live_until) if self.live_until is not None else until

    def is_fresh(self, now: datetime, ttl: timedelta) -> bool:
        return now < self.fresh_until(ttl)

    def to_json(self) -> dict[str, Any]:
        return {
            "lat": self.latitude,
            "lng": self.longitude,
            "at": self.updated_at.isoformat(),
            "live_until": self.live_until.isoformat() if self.live_until else None,
            "message_id": self.message_id,
            "source": "telegram",
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> CurrentOrigin | None:
        try:
            live_until = value.get("live_until")
            return cls(
                float(value["lat"]),
                float(value["lng"]),
                datetime.fromisoformat(value["at"]),
                datetime.fromisoformat(live_until) if live_until else None,
                int(value["message_id"]) if value.get("message_id") is not None else None,
            )
        except (KeyError, TypeError, ValueError):
            return None


def distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class LocationUpdate:
    latitude: float
    longitude: float
    sent_at: datetime
    live_until: datetime | None
    message_id: int | None
    edited: bool

    @property
    def stopped(self) -> bool:
        """A live location that stopped: Telegram edits the message and drops `live_period`."""
        return self.edited and self.live_until is None


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def location_update(message: dict[str, Any], now: datetime, *, edited: bool = False) -> LocationUpdate | None:
    """A Telegram message (or edited message) with `location` → a validated update; anything else → None."""
    location = message.get("location")
    if not isinstance(location, dict):
        return None
    try:
        latitude, longitude = float(location["latitude"]), float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    sent = _timestamp(message.get("date")) or now
    at = (_timestamp(message.get("edit_date")) if edited else None) or sent
    live_until = None
    live_period = location.get("live_period")
    if isinstance(live_period, int) and live_period > 0:
        live_until = sent + timedelta(seconds=live_period)  # relative to the message sending date
    message_id = message.get("message_id")
    return LocationUpdate(
        latitude,
        longitude,
        min(at, now),
        live_until,
        int(message_id) if isinstance(message_id, int) else None,
        edited,
    )


async def load_origin(session: AsyncSession) -> CurrentOrigin | None:
    pref = await session.get(UserPreference, ORIGIN_PREF_KEY)
    return CurrentOrigin.from_json(pref.value) if pref is not None else None


async def save_origin(session: AsyncSession, origin: CurrentOrigin) -> None:
    stmt = insert(UserPreference).values(key=ORIGIN_PREF_KEY, value=origin.to_json())
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[UserPreference.key], set_={"value": stmt.excluded.value})
    )


async def forget_origin(session: AsyncSession) -> bool:
    """«Я дома»: back to DEFAULT_ORIGIN. True if a point was stored."""
    result = await session.execute(
        delete(UserPreference).where(UserPreference.key == ORIGIN_PREF_KEY).returning(UserPreference.key)
    )
    return result.first() is not None


@dataclass(frozen=True)
class OriginChange:
    origin: CurrentOrigin | None  # what is stored now (None: forgotten)
    ignored: bool = False  # an update of a message Jarvis no longer follows, or an out-of-order one


async def apply_location(session: AsyncSession, update: LocationUpdate) -> OriginChange:
    current = await load_origin(session)
    follows = current is not None and current.live and current.message_id == update.message_id
    if update.stopped:
        if follows:
            await forget_origin(session)
            return OriginChange(None)
        return OriginChange(current, ignored=True)
    if update.edited and not follows:
        # A live update of a message Jarvis no longer follows: a newer location was sent or «я дома» was said.
        return OriginChange(current, ignored=True)
    if current is not None and update.sent_at < current.updated_at:
        return OriginChange(current, ignored=True)
    latitude, longitude = update.latitude, update.longitude
    if current is not None:
        moved = distance_meters(current.latitude, current.longitude, latitude, longitude)
        if moved < MIN_MOVE_METERS:
            latitude, longitude = current.latitude, current.longitude
    origin = CurrentOrigin(latitude, longitude, update.sent_at, update.live_until, update.message_id)
    await save_origin(session, origin)
    return OriginChange(origin)


def origin_status(origin: CurrentOrigin | None, now: datetime, ttl: timedelta, tz: ZoneInfo) -> str:
    """One line for `jarvis doctor` (no coordinates)."""
    if origin is None:
        return "no location shared in Telegram → DEFAULT_ORIGIN"
    kind = "live location" if origin.live else "location"
    age = max(0, int((now - origin.updated_at).total_seconds() // 60))
    if origin.is_fresh(now, ttl):
        until = format_local(origin.fresh_until(ttl), tz)
        return f"Telegram {kind}, updated {age} min ago, used until {until}"
    return f"Telegram {kind} from {age} min ago is stale → DEFAULT_ORIGIN"
