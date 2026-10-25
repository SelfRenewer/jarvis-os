"""«🧭 Маршрут в 2ГИС»: an inline URL button with the route from the current starting point to the meeting.

Shown on travel reminders and on «когда выходить?» when both ends have coordinates: a location shared in Telegram
as is, addresses through the cached geocoder (`TravelService.locate`). Without coordinates there is no button
(Jarvis never guesses a point). Building it never fails a reminder: any problem → no button. Coordinates and
addresses are never logged.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import CalendarEvent
from jarvis.integrations.maps.routes import RouteStatus
from jarvis.integrations.maps.twogis import route_url
from jarvis.integrations.telegram.client import UrlButton, inline_keyboard
from jarvis.reminders.service import SOURCE_EVENT, FireDecision
from jarvis.reminders.travel import TravelService

if TYPE_CHECKING:
    from jarvis.services.container import Container

log = logging.getLogger(__name__)

ROUTE_BUTTON = "🧭 Маршрут в 2ГИС"


async def route_markup(
    travel: TravelService, session: AsyncSession, origin: str | None, destination: str | None, now: datetime
) -> dict[str, Any] | None:
    """Inline keyboard with the 2GIS route button, or None when either end has no coordinates."""
    if not origin or not destination:
        return None
    try:
        start = await travel.locate(session, origin, now)
        if start.status != RouteStatus.OK or start.point is None:
            return None
        end = await travel.locate(session, destination, now)
        if end.status != RouteStatus.OK or end.point is None:
            return None
        settings = travel.settings
        url = route_url(start.point, end.point, settings.maps_travel_mode, settings.maps_2gis_base_url)
        return inline_keyboard([[UrlButton(ROUTE_BUTTON, url)]])
    except Exception as exc:  # a convenience link must never block a reminder or a reply
        log.warning("2GIS route button skipped: %s", type(exc).__name__)
        return None


async def event_route_markup(
    travel: TravelService, session: AsyncSession, event: CalendarEvent, now: datetime
) -> dict[str, Any] | None:
    """From where the user will set off (shared location / previous meeting / home) to the event's address."""
    if not event.location or event.start_at is None:
        return None
    origin = await travel.resolve_origin(session, event, now=now)
    return await route_markup(travel, session, origin, event.location, now)


async def reminder_route_markup(container: Container, decision: FireDecision, now: datetime) -> dict[str, Any] | None:
    """The button for a firing reminder of a calendar event with an address (task reminders have no place)."""
    event = decision.event
    if decision.reminder.source_type != SOURCE_EVENT or not isinstance(event, CalendarEvent) or not event.location:
        return None
    try:
        async with container.db.session() as session:
            return await event_route_markup(container.travel, session, event, now)
    except Exception as exc:
        log.warning("2GIS route button skipped: %s", type(exc).__name__)
        return None
