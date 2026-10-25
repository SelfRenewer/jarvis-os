"""Travel time: cost guards of the estimate cache and the starting point shared from Telegram (fakes only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from jarvis.bot.intents import Intent, keyword_route
from jarvis.bot.location import FORWARDED_LOCATION
from jarvis.bot.service import BotService
from jarvis.db.models import CalendarEvent, Reminder, ScheduledJob, TelegramUpdate, TravelEstimate, UsageCounter
from jarvis.integrations.maps.routes import RouteStatus, UnavailableRoutesApi, format_coordinates
from jarvis.reminders.origin import CurrentOrigin, load_origin, location_update, origin_status
from jarvis.reminders.travel import TravelService
from jarvis.scheduler.queue import enqueue
from jarvis.scheduler.runner import Worker

pytestmark = pytest.mark.db
TZ = ZoneInfo("Asia/Tbilisi")
DEST = "Tbilisi Boulevard 5"
USER = 1001
LAT, LNG = 41.7399, 44.8266  # somewhere in the city
HERE = format_coordinates(LAT, LNG)


def local(h: int, m: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def location_msg(
    update_id: int,
    lat: float = LAT,
    lng: float = LNG,
    *,
    sent: datetime,
    message_id: int = 10,
    live_period: int | None = None,
    edited_at: datetime | None = None,
    forwarded: bool = False,
    user: int = USER,
) -> dict[str, Any]:
    location: dict[str, Any] = {"latitude": lat, "longitude": lng, "horizontal_accuracy": 12}
    if live_period is not None:
        location["live_period"] = live_period
    body: dict[str, Any] = {
        "message_id": message_id,
        "from": {"id": user},
        "chat": {"id": user, "type": "private"},
        "date": int(sent.timestamp()),
        "location": location,
    }
    if forwarded:
        body["forward_origin"] = {"type": "hidden_user", "sender_user_name": "Someone", "date": int(sent.timestamp())}
    if edited_at is not None:
        body["edit_date"] = int(edited_at.timestamp())
        return {"update_id": update_id, "edited_message": body}
    return {"update_id": update_id, "message": body}


def text_msg(text: str, update_id: int) -> dict[str, Any]:
    chat = {"id": USER, "type": "private"}
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "text": text, "from": {"id": USER}, "chat": chat},
    }


async def run_job(c: Any, worker: Worker, job_type: str) -> None:
    async with c.db.session() as s:
        await enqueue(s, job_type=job_type, run_at=c.clock.now(), payload={})
    await worker.drain()


async def add_event(c: Any, worker: Worker, summary: str, start: datetime, location: str | None = DEST) -> None:
    raw: dict[str, Any] = {"summary": summary, "start": {"dateTime": iso(start)}}
    raw["end"] = {"dateTime": iso(start + timedelta(minutes=30))}
    if location:
        raw["location"] = location
    c.calendar.add_event("primary", **raw)
    await run_job(c, worker, "calendar.reconcile")


async def origin_for(c: Any, summary: str) -> str | None:
    async with c.db.session() as s:
        event = await s.scalar(select(CalendarEvent).where(CalendarEvent.summary == summary))
        assert event is not None
        return await c.travel.resolve_origin(s, event, now=c.clock.now())


async def stored(c: Any) -> CurrentOrigin | None:
    async with c.db.session() as s:
        return await load_origin(s)


async def leave_reminder(c: Any) -> Reminder | None:
    async with c.db.session() as s:
        return await s.scalar(
            select(Reminder).where(
                Reminder.kind == "leave", Reminder.offset_minutes == 0, Reminder.status == "scheduled"
            )
        )


# --- estimate cache -------------------------------------------------------------------------------------------


async def test_unavailable_answer_is_not_cached(container: Any) -> None:
    """An outage (or a key added later) must not block travel time for 12 h: only answers are cached."""
    c = container
    origin = c.settings.default_origin
    c.routes.default_status = RouteStatus.UNAVAILABLE
    now = c.clock.now()
    async with c.db.session() as s:
        first = await c.travel.estimate(s, origin=origin, destination=DEST, departure=local(15), now=now)
    assert first.status == RouteStatus.UNAVAILABLE
    c.routes.durations[(origin, DEST)] = 900
    async with c.db.session() as s:
        second = await c.travel.estimate(s, origin=origin, destination=DEST, departure=local(15), now=now)
    assert (second.status, second.duration_seconds) == (RouteStatus.OK, 900)
    async with c.db.session() as s:
        third = await c.travel.estimate(s, origin=origin, destination=DEST, departure=local(15), now=now)
    assert third.duration_seconds == 900
    assert len(c.routes.calls) == 2  # the OK answer came from the cache


async def test_no_key_skips_request_budget_and_cache(container: Any) -> None:
    c = container
    travel = TravelService(UnavailableRoutesApi(), c.settings)
    async with c.db.session() as s:
        info = await travel.estimate(
            s, origin=c.settings.default_origin, destination=DEST, departure=local(15), now=c.clock.now()
        )
    assert info.status == RouteStatus.UNAVAILABLE
    assert "not configured" in (info.note or "")
    async with c.db.session() as s:
        assert (await s.scalars(select(TravelEstimate))).all() == []
        assert (await s.scalars(select(UsageCounter))).all() == []


async def test_cache_key_does_not_depend_on_now(container: Any) -> None:
    c = container
    origin = c.settings.default_origin
    c.routes.durations[(origin, DEST)] = 600
    for minutes in (0, 5, 40):
        async with c.db.session() as s:
            await c.travel.estimate(
                s, origin=origin, destination=DEST, departure=local(15), now=c.clock.now() + timedelta(minutes=minutes)
            )
    assert len(c.routes.calls) == 1


# --- a location shared in Telegram -----------------------------------------------------------------------------


async def test_location_is_the_origin_while_fresh(container: Any, worker: Worker) -> None:
    c = container
    bot = BotService(c)
    await add_event(c, worker, "Скоро", local(10, 30))  # departure ≈ 09:30: inside the 2 h window
    await add_event(c, worker, "Вечером", local(19, 0))  # departure ≈ 18:00: the point will be stale by then
    assert await bot.handle_update(location_msg(1, sent=c.clock.now())) == "processed"
    reply = c.telegram.texts()[-1]
    assert reply.startswith("📍 Принял, считаю дорогу от этой точки до 11:00.")
    markup = c.telegram.sent[-1]["reply_markup"]
    assert [[b["text"] for b in row] for row in markup["inline_keyboard"]] == [["🏠 Я дома"]]  # a tap, not typing
    assert str(LAT) not in reply
    origin = await stored(c)
    assert origin is not None and origin.value == HERE and not origin.live
    async with c.db.session() as s:
        jobs = (await s.scalars(select(ScheduledJob).where(ScheduledJob.job_type == "reminders.plan"))).all()
    assert any(j.payload.get("coalesce") == "origin" and j.status == "pending" for j in jobs)
    assert await origin_for(c, "Скоро") == HERE
    assert await origin_for(c, "Вечером") == c.settings.default_origin
    c.clock.set(local(11, 1))  # TTL (120 min) passed
    await add_event(c, worker, "Днём", local(12, 0))
    assert await origin_for(c, "Днём") == c.settings.default_origin


async def test_home_button_forgets_the_point(container: Any) -> None:
    """The user's request 2026-10-14: «просто добавь кнопку „я дома“» under the location reply."""
    c = container
    bot = BotService(c)
    await bot.handle_update(location_msg(1, sent=c.clock.now()))
    button = c.telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    tap = {
        "update_id": 2,
        "callback_query": {
            "id": "cb2",
            "data": button,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }
    await bot.handle_update(tap)
    assert c.telegram.texts()[-1] == "🏠 Хорошо, считаю дорогу от дома."
    assert c.telegram.answered[-1][1] == "🏠 Считаю от дома"
    assert await stored(c) is None
    assert c.telegram.markup_edits[-1]["reply_markup"] is None  # the button disappears after the tap


async def test_reminders_and_leave_follow_the_shared_location(container: Any, worker: Worker) -> None:
    c = container
    bot = BotService(c)
    c.routes.durations[(c.settings.default_origin, DEST)] = 40 * 60
    c.routes.durations[(HERE, DEST)] = 20 * 60
    await add_event(c, worker, "Встреча", local(10, 30))
    await bot.handle_update(text_msg("/leave", 1))
    assert c.telegram.texts()[-1] == (
        "🚶 Выходить на «Встреча» в 09:40 — через 40 мин (дорога ~40 мин от дома + запас 10 мин)."
    )
    await bot.handle_update(location_msg(2, sent=c.clock.now()))
    await worker.drain()  # the coalesced reminders.plan enqueued for the new starting point
    leave = await leave_reminder(c)
    assert leave is not None and leave.payload["origin"] == HERE
    assert leave.fire_at == local(10, 0).astimezone(UTC)  # 10:30 − 20 min − 10 min buffer
    await bot.handle_update(text_msg("когда выходить?", 3))
    assert c.telegram.texts()[-1] == (
        "🚶 Выходить на «Встреча» в 10:00 — через 60 мин (дорога ~20 мин от вашей геопозиции + запас 10 мин)."
    )
    await bot.handle_update(text_msg("Я дома", 4))
    assert c.telegram.texts()[-1] == "🏠 Хорошо, считаю дорогу от дома."
    assert await stored(c) is None
    await worker.drain()
    leave = await leave_reminder(c)
    assert leave is not None and leave.payload["origin"] == c.settings.default_origin


async def test_live_location_updates_are_silent_and_stop(container: Any) -> None:
    c = container
    bot = BotService(c)
    start = c.clock.now()
    await bot.handle_update(location_msg(1, sent=start, message_id=50, live_period=3600))
    assert c.telegram.texts()[-1].startswith("📍 Принял трансляцию — считаю дорогу от неё, пока она идёт (до 10:00).")
    sent_before = len(c.telegram.sent)
    c.clock.set(local(9, 5))
    far = location_msg(2, LAT + 0.009, LNG, sent=start, message_id=50, live_period=3600, edited_at=c.clock.now())
    assert await bot.handle_update(far) == "processed"
    moved = await stored(c)
    assert moved is not None and moved.value == format_coordinates(LAT + 0.009, LNG) and moved.live
    c.clock.set(local(9, 7))
    near = location_msg(3, LAT + 0.0094, LNG, sent=start, message_id=50, live_period=3600, edited_at=c.clock.now())
    await bot.handle_update(near)
    jitter = await stored(c)
    assert jitter is not None and jitter.value == moved.value  # < 300 m: same point, the travel cache keeps hitting
    assert jitter.updated_at == local(9, 7).astimezone(UTC)
    assert len(c.telegram.sent) == sent_before  # live updates never answer
    ttl = c.travel.origin_ttl
    assert jitter.is_fresh(local(9, 59), ttl) and not jitter.is_fresh(local(10, 0), ttl)  # the sharing window ended
    c.clock.set(local(9, 20))
    stop = location_msg(4, LAT + 0.0094, LNG, sent=start, message_id=50, edited_at=c.clock.now())  # no live_period
    await bot.handle_update(stop)
    assert await stored(c) is None
    assert len(c.telegram.sent) == sent_before


async def test_live_updates_of_an_abandoned_message_are_ignored(container: Any) -> None:
    c = container
    bot = BotService(c)
    start = c.clock.now()
    await bot.handle_update(location_msg(1, sent=start, message_id=50, live_period=3600))
    await bot.handle_update(text_msg("я дома", 2))
    c.clock.set(local(9, 10))
    await bot.handle_update(
        location_msg(3, LAT + 0.01, LNG, sent=start, message_id=50, live_period=3600, edited_at=c.clock.now())
    )
    assert await stored(c) is None  # «я дома» wins until a new location is sent
    await bot.handle_update(location_msg(4, sent=c.clock.now(), message_id=60))
    await bot.handle_update(
        location_msg(5, LAT + 0.02, LNG, sent=start, message_id=50, live_period=3600, edited_at=c.clock.now())
    )
    current = await stored(c)
    assert current is not None and current.value == HERE and current.message_id == 60


async def test_event_in_between_wins_over_the_shared_location(container: Any, worker: Worker) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(location_msg(1, sent=c.clock.now()))
    await add_event(c, worker, "Офис", local(9, 30), location="Office, Tbilisi")  # ends 10:00, after now
    await add_event(c, worker, "Встреча", local(10, 30))
    assert await origin_for(c, "Встреча") == "Office, Tbilisi"
    c.clock.set(local(10, 5))  # the office meeting is over: a fresh point is better than its address
    await bot.handle_update(location_msg(2, sent=c.clock.now(), message_id=11))
    assert await origin_for(c, "Встреча") == HERE


async def test_forwarded_location_is_not_the_users_position(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(location_msg(1, sent=c.clock.now(), forwarded=True))
    assert c.telegram.texts()[-1] == FORWARDED_LOCATION
    assert await stored(c) is None


async def test_edited_text_is_never_run_again(container: Any) -> None:
    c = container
    bot = BotService(c)
    update = text_msg("/help", 1)
    update["edited_message"] = update.pop("message")
    assert await bot.handle_update(update) == "processed"
    assert c.telegram.sent == []
    async with c.db.session() as s:
        row = await s.get(TelegramUpdate, 1)
    assert row is not None and row.status == "processed"


async def test_unauthorized_live_location_is_ignored(container: Any) -> None:
    c = container
    bot = BotService(c)
    update = location_msg(1, sent=c.clock.now(), user=999, live_period=600, edited_at=c.clock.now())
    assert await bot.handle_update(update) == "unauthorized"
    assert await stored(c) is None
    assert c.telegram.sent == []


async def test_home_command_and_leave_without_maps_key(container: Any, worker: Worker) -> None:
    c = container
    c.travel.routes = UnavailableRoutesApi()
    bot = BotService(c)
    await add_event(c, worker, "Врач", local(11, 0))
    await bot.handle_update(location_msg(1, sent=c.clock.now()))
    assert "Ключ карт пока не подключён" in c.telegram.texts()[-1]
    await bot.handle_update(text_msg("/leave", 2))
    assert (
        c.telegram.texts()[-1] == f"«Врач» в 11:00, 📍 {DEST}.\nВремя в пути не считаю: ключ Google Maps не подключён."
    )
    await bot.handle_update(text_msg("/home", 3))
    assert c.telegram.texts()[-1] == "🏠 Хорошо, считаю дорогу от дома."
    assert await stored(c) is None


def test_location_phrases_route_without_llm() -> None:
    for phrase in ("Когда выходить?", "во сколько мне выезжать", "Через сколько мне выходить?", "а когда выйти?"):
        assert keyword_route(phrase).intent == Intent.LEAVE_TIME, phrase
    for phrase in ("Я дома", "Джарвис, я уже дома.", "вернулся домой", "забудь мою геопозицию", "сбрось геолокацию"):
        assert keyword_route(phrase).intent == Intent.ORIGIN_RESET, phrase
    assert keyword_route("я не дома").intent != Intent.ORIGIN_RESET
    assert keyword_route("запусти стоматологию, я не дома").intent != Intent.ORIGIN_RESET
    assert keyword_route("удали задачу купить хлеб").intent == Intent.DELETE_TASK
    assert keyword_route("напомни выйти из дома в 9").intent != Intent.LEAVE_TIME


def test_location_update_parsing() -> None:
    now = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)
    sent = now - timedelta(minutes=3)
    update = location_update(location_msg(1, sent=sent, live_period=0x7FFFFFFF)["message"], now)
    assert update is not None and update.sent_at == sent and update.live_until is not None
    assert not update.stopped
    future = location_update(location_msg(1, sent=now + timedelta(minutes=5))["message"], now)
    assert future is not None and future.sent_at == now  # clock skew: never in the future
    assert location_update({"location": {"latitude": 123, "longitude": 1}}, now) is None
    assert location_update({"location": {"latitude": "x"}}, now) is None
    assert location_update({"text": "hi"}, now) is None
    edited = location_msg(1, sent=sent, edited_at=now - timedelta(minutes=1))["edited_message"]
    stop = location_update(edited, now, edited=True)
    assert stop is not None and stop.stopped and stop.sent_at == now - timedelta(minutes=1)


def test_origin_status_for_doctor_has_no_coordinates() -> None:
    now = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)
    ttl = timedelta(minutes=120)
    assert "DEFAULT_ORIGIN" in origin_status(None, now, ttl, TZ)
    line = origin_status(CurrentOrigin(LAT, LNG, now - timedelta(minutes=10)), now, ttl, TZ)
    assert line == "Telegram location, updated 10 min ago, used until 10:50"
    assert str(LAT) not in line
    live = CurrentOrigin(LAT, LNG, now - timedelta(minutes=1), live_until=now + timedelta(minutes=30))
    assert origin_status(live, now, ttl, TZ) == "Telegram live location, updated 1 min ago, used until 09:30"
    stale = CurrentOrigin(LAT, LNG, now - timedelta(hours=3))
    assert origin_status(stale, now, ttl, TZ) == "Telegram location from 180 min ago is stale → DEFAULT_ORIGIN"
