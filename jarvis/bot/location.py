"""Where the user is, for travel time: a location shared in Telegram, «я дома», «когда выходить?».

A shared location (📎 → Геопозиция, or a reply-keyboard button with `request_location`) becomes the starting point
for travel time while it is fresh (`jarvis/reminders/origin.py`); live-location updates (`edited_message`) are
silent. Coordinates are never logged and never shown back.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.agenda.service import human_minutes
from jarvis.bot import fmt
from jarvis.core.clock import format_local, to_local
from jarvis.db.models import CalendarEvent, TodoistTask
from jarvis.integrations.maps.routes import GeoPoint, RouteStatus, parse_coordinates
from jarvis.integrations.maps.twogis import route_url
from jarvis.integrations.telegram.client import InlineButton, UrlButton
from jarvis.reminders.origin import apply_location, forget_origin, location_update
from jarvis.reminders.policy import compute_leave_at
from jarvis.reminders.route_link import event_route_markup
from jarvis.scheduler.queue import enqueue_coalesced
from jarvis.services.container import Container
from jarvis.sync.links import done_by_tasks, linked_tasks

log = logging.getLogger(__name__)

FORWARDED_LOCATION = "↪️ Это пересланная геопозиция — за свою точку её не принимаю. Свою: 📎 → Геопозиция."
NOT_A_LOCATION = "Не разобрал геопозицию — пришлите её ещё раз: 📎 → Геопозиция."
NO_KEY_NOTE = "ℹ️ Ключ карт пока не подключён — время в пути начну считать, когда он появится."
HOME_BUTTON = "🏠 Я дома"
HOME_CALLBACK = "oh:home"
AT_HOME_METERS = 250  # closer than this to DEFAULT_ORIGIN: «вы дома»
TRIP_HOURS = 24  # events with an address this far ahead are listed under «Куда ехать»
MODE_WORDS = {"DRIVE": "на машине", "WALK": "пешком", "BICYCLE": "на велосипеде", "TWO_WHEELER": "на скутере"}
# Tasks that usually mean going out («купить …», «забрать …», «стоматолог»…) — a hint, not a rule.
OUTSIDE = re.compile(
    r"купит|забрат|отвез|отнес|съезд|сходит|поехат|заехат|зайти|записат|встрет|магазин|аптек|банк|почт|стоматолог"
    r"|врач|барбер|постри|сдат|получит|ресторан|поужина|пообеда|кафе|экзамен|права",
    re.IGNORECASE,
)


def meters_between(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance (haversine) — enough to tell «at home» from «elsewhere»."""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat, dlon = lat2 - lat1, math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class TravelDesk:
    def __init__(self, container: Container) -> None:
        self.c = container

    async def _replan(self, session: AsyncSession, now: datetime) -> None:
        # Leave reminders and the planner's travel blocks follow the new starting point (idempotent job).
        await enqueue_coalesced(session, job_type="reminders.plan", run_at=now, coalesce_key="origin", priority=40)

    async def on_location(self, message: dict[str, Any], *, edited: bool = False) -> tuple[str, list[list[Any]]] | None:
        """Store the point; (reply, button rows) for a new location — where you are, how far home, where to go today —
        None for a live update (those stay silent)."""
        now = self.c.clock.now()
        update = location_update(message, now, edited=edited)
        if update is None:
            return None if edited else (NOT_A_LOCATION, [])
        async with self.c.db.session() as s:
            change = await apply_location(s, update)
            if not change.ignored and (not edited or update.stopped):
                await self._replan(s, now)
        log.info("location %s", "stopped" if update.stopped else ("updated" if edited else "received"))
        if edited:
            return None
        origin = change.origin
        ttl = self.c.travel.origin_ttl
        tz = self.c.settings.tz
        if origin is None or not origin.is_fresh(now, ttl):
            return "📍 Эта геопозиция уже устарела — считаю дорогу как обычно.", []
        if origin.live and origin.live_until is not None:
            ends_soon = origin.live_until - now <= timedelta(days=1)  # "forever" live locations have no end time
            window = f" (до {format_local(origin.live_until, tz)})" if ends_soon else ""
            lines = [f"📍 Принял трансляцию — считаю дорогу от неё, пока она идёт{window}."]
        else:
            lines = [f"📍 Принял, считаю дорогу от этой точки до {format_local(origin.fresh_until(ttl), tz)}."]
        if not self.c.travel.routes.configured:
            lines.append(NO_KEY_NOTE)
        rows: list[list[Any]] = []
        try:
            async with self.c.db.session() as s:
                summary, rows = await self.location_summary(s, origin, now)
        except Exception as exc:  # the point is stored anyway; the summary is a convenience
            log.warning("location summary failed: %s", type(exc).__name__)
            summary = []
        rows.append([InlineButton(HOME_BUTTON, HOME_CALLBACK)])  # a tap instead of typing «я дома»
        return fmt.blocks(fmt.b(lines[0]), *summary, fmt.lines(*(fmt.hint(x) for x in lines[1:]))), rows

    async def location_summary(
        self, session: AsyncSession, origin: Any, now: datetime
    ) -> tuple[list[Any], list[list[Any]]]:
        """«📍 Вы здесь», «🏠 До дома ~N мин» (or «вы дома»), «🚗 Куда ехать» (events with an address, leave times),
        «🏃 Дела вне дома» (tasks that look like going out) and the shopping list size, with 2GIS buttons."""
        settings, travel = self.c.settings, self.c.travel
        tz, mode = settings.tz, settings.maps_travel_mode
        here = GeoPoint(origin.latitude, origin.longitude)
        blocks: list[Any] = []
        rows: list[list[Any]] = []
        where: list[Any] = []
        reverse = getattr(travel.routes, "reverse", None)
        if reverse is not None:
            place = await reverse(here.latitude, here.longitude)
            if place:
                where.append(fmt.line("📍", fmt.b("Вы здесь:"), place))
        if settings.default_origin and travel.routes.configured:
            home = await travel.locate(session, settings.default_origin, now)
            if home.status == RouteStatus.OK and home.point is not None:
                if meters_between(here, home.point) < AT_HOME_METERS:
                    where.append(fmt.line("🏠", fmt.b("Вы дома.")))
                else:
                    info = await travel.estimate(
                        session, origin=here.value, destination=settings.default_origin, departure=now, now=now
                    )
                    if info.status == RouteStatus.OK and info.duration_seconds is not None:
                        minutes = -(-info.duration_seconds // 60)
                        road = f"~{human_minutes(minutes)} {MODE_WORDS.get(mode.upper(), '')}".rstrip()
                        where.append(fmt.line("🏠", fmt.b("До дома"), road))
                    url = route_url(here, home.point, mode, settings.maps_2gis_base_url)
                    rows.append([UrlButton("🧭 Домой в 2ГИС", url)])
        blocks.append(fmt.lines(*where))
        trips: list[Any] = []
        events = (
            await session.scalars(
                select(CalendarEvent)
                .where(
                    CalendarEvent.start_at > now,
                    CalendarEvent.start_at <= now + timedelta(hours=TRIP_HOURS),
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.all_day.is_(False),
                    CalendarEvent.self_declined.is_(False),
                    CalendarEvent.location.is_not(None),
                    CalendarEvent.location != "",
                )
                .order_by(CalendarEvent.start_at)
                .limit(3)
            )
        ).all()
        today = to_local(now, tz).date()
        for event in events:
            assert event.start_at is not None
            start = to_local(event.start_at, tz)
            when = start.strftime("%H:%M") if start.date() == today else start.strftime("%d.%m %H:%M")
            trip = await self.c.reminders.travel_for(session, event, now)
            note: str | None = event.location
            if trip is not None and trip.status == RouteStatus.OK and trip.duration_seconds is not None:
                leave_at = compute_leave_at(
                    event.start_at, trip.duration_seconds, settings.reminder_travel_buffer_minutes
                )
                note = (
                    f"~{-(-trip.duration_seconds // 60)} мин {self.origin_phrase(trip.origin)}, "
                    f"выходить в {format_local(leave_at, tz)}"
                )
            trips.append(fmt.bullet(f"{when} {event.summary or 'Встреча'}", note))
        if events and (markup := await event_route_markup(travel, session, events[0], now)):
            rows.append([UrlButton(f"🧭 К «{(events[0].summary or 'встрече')[:24]}» в 2ГИС", b["url"])
                         for row in markup["inline_keyboard"] for b in row if "url" in b][:1])  # fmt: skip
        blocks.append(fmt.section("Куда ехать", trips, emoji="🚗"))
        tasks = (
            await session.scalars(
                select(TodoistTask)
                .where(TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False))
                .where((TodoistTask.due_date <= today + timedelta(days=1)) | TodoistTask.due_date.is_(None))
                .order_by(TodoistTask.due_date.nulls_last(), TodoistTask.content)
            )
        ).all()
        errands = [t for t in tasks if OUTSIDE.search(t.content or "")][:5]
        blocks.append(
            fmt.section(
                "Дела вне дома",
                [fmt.bullet(t.content, t.due_date.strftime("%d.%m") if t.due_date else None) for t in errands],
                emoji="🏃",
            )
        )
        shopping = await self.c.shopping.open_items(session)
        if shopping:
            blocks.append(fmt.line("🛒", f"В покупках {len(shopping)} —", fmt.hint("«иду в магазин» покажет список")))
        if not trips and not errands:
            blocks.append(fmt.hint("Встреч с адресом и дел вне дома на сегодня нет."))
        return blocks, [row for row in rows if row]

    async def forget(self) -> str:
        """«Я дома»: the starting point is DEFAULT_ORIGIN again."""
        now = self.c.clock.now()
        async with self.c.db.session() as s:
            if await forget_origin(s):
                await self._replan(s, now)
        if self.c.settings.default_origin:
            return "🏠 Хорошо, считаю дорогу от дома."
        return "🏠 Забыл геопозицию. Адрес дома не задан (DEFAULT_ORIGIN) — дорогу считаю только от присланной точки."

    def origin_phrase(self, origin: str | None) -> str:
        if not origin:
            return ""
        if parse_coordinates(origin) is not None:
            return "от вашей геопозиции"
        if origin == self.c.settings.default_origin:
            return "от дома"
        return f"от «{origin}»"

    async def _leave_event(self, session: AsyncSession, now: datetime) -> CalendarEvent | None:
        """The next event with an address in the reminder horizon that is not done in Todoist."""
        settings = self.c.settings
        event = await session.scalar(
            select(CalendarEvent)
            .where(
                CalendarEvent.start_at > now,
                CalendarEvent.start_at <= now + timedelta(hours=settings.reminder_horizon_hours),
                CalendarEvent.status != "cancelled",
                CalendarEvent.all_day.is_(False),
                CalendarEvent.self_declined.is_(False),
                CalendarEvent.jarvis_managed.is_(False),
                CalendarEvent.location.is_not(None),
                CalendarEvent.location != "",
            )
            .order_by(CalendarEvent.start_at)
            .limit(1)
        )
        if event is None or event.start_at is None:
            return None
        if done_by_tasks(event, await linked_tasks(session, event, settings.tz), settings.tz):
            return None  # done in Todoist: no reminders for it either
        return event

    async def route_markup(self, session: AsyncSession, now: datetime) -> dict[str, Any] | None:
        """«🧭 Маршрут в 2ГИС» for the event `leave_text` talks about (None: no such event or no coordinates)."""
        event = await self._leave_event(session, now)
        return await event_route_markup(self.c.travel, session, event, now) if event is not None else None

    async def leave_text(self, session: AsyncSession, now: datetime) -> str | None:
        """«Когда выходить?» for the next event with an address, from the current starting point (None: no such
        event in the reminder horizon). Uses the same cached estimate as the reminders — no guessing."""
        event = await self._leave_event(session, now)
        if event is None or event.start_at is None:
            return None
        settings = self.c.settings
        tz = settings.tz
        summary = event.summary or "встреча"
        start_local = format_local(event.start_at, tz)
        info = await self.c.reminders.travel_for(session, event, now)
        if info is None:
            return None
        if info.status == RouteStatus.OK and info.duration_seconds is not None:
            buffer = settings.reminder_travel_buffer_minutes
            leave_at = compute_leave_at(event.start_at, info.duration_seconds, buffer)
            minutes = int((leave_at - now).total_seconds() // 60)
            road = f"дорога ~{-(-info.duration_seconds // 60)} мин {self.origin_phrase(info.origin)}".rstrip()
            if minutes <= 0:
                return f"🚶 Пора выходить на «{summary}» — начало в {start_local} ({road})."
            return (
                f"🚶 Выходить на «{summary}» в {format_local(leave_at, tz)} — через {human_minutes(minutes)} "
                f"({road} + запас {buffer} мин)."
            )
        head = f"«{summary}» в {start_local}, 📍 {event.location}."
        if info.origin is None:
            return f"{head}\nНе знаю, откуда ехать: пришлите геопозицию (📎 → Геопозиция) или задайте DEFAULT_ORIGIN."
        if not self.c.travel.routes.configured:
            return f"{head}\nВремя в пути не считаю: ключ Google Maps не подключён."
        maps = self.c.travel.routes.label  # the active provider (Google Maps / OpenRouteService)
        reason = {
            RouteStatus.NOT_FOUND: f"{maps} не нашёл дорогу до этого адреса.",
            RouteStatus.QUOTA: f"Лимит запросов к {maps} на сегодня исчерпан.",
        }.get(info.status, f"{maps} сейчас не отвечает.")
        return f"{head}\n{reason} Я не буду угадывать — проверь дорогу сам."
