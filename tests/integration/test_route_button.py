"""«🧭 Маршрут в 2ГИС»: the URL builder, URL buttons, and the button on travel reminders and «когда выходить?»."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from jarvis.bot.service import BotService
from jarvis.core.clock import FrozenClock
from jarvis.core.security import CallbackSigner
from jarvis.desktop.channel import _buttons
from jarvis.integrations.maps.routes import FakeRoutesApi, GeoPoint, format_coordinates
from jarvis.integrations.maps.twogis import route_url
from jarvis.integrations.telegram.client import FakeTelegramApi, InlineButton, UrlButton, inline_keyboard
from jarvis.reminders.route_link import ROUTE_BUTTON
from jarvis.scheduler.queue import enqueue
from jarvis.scheduler.runner import Worker

TZ = ZoneInfo("Asia/Tbilisi")
USER = 1001
DEST = "Vake Park, Tbilisi"
HOME = GeoPoint(41.7443, 44.8304)
DEST_POINT = GeoPoint(41.7151, 44.8271)  # lat ≠ lng here, so a swapped order would show
EXPECTED = "https://2gis.ge/tbilisi/directions/points/44.830400%2C41.744300%7C44.827100%2C41.715100"


def local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 23, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def text_msg(text: str, update_id: int) -> dict[str, Any]:
    chat = {"id": USER, "type": "private"}
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "text": text, "from": {"id": USER}, "chat": chat},
    }


def url_buttons(markup: dict[str, Any] | None) -> list[dict[str, str]]:
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row if "url" in b]


# --- builder and buttons (no DB) --------------------------------------------------------------------------------


def test_route_url_is_lon_lat_and_follows_the_mode() -> None:
    assert route_url(HOME, DEST_POINT) == EXPECTED
    assert route_url(HOME, DEST_POINT, "walk", "https://2gis.ge/tbilisi/") == EXPECTED.replace(
        "/directions/points/", "/directions/tab/pedestrian/points/"
    )
    assert "/directions/tab/bus/points/" in route_url(HOME, DEST_POINT, "TRANSIT")
    assert "/directions/points/" in route_url(HOME, DEST_POINT, "BICYCLE")  # no bicycle tab on 2GIS: by car
    assert route_url(HOME, DEST_POINT, base_url="https://2gis.ge/tbilisi").startswith("https://2gis.ge/tbilisi/")


def test_url_buttons_are_not_signed_and_reach_telegram_as_links() -> None:
    with pytest.raises(ValueError, match="http"):
        UrlButton(ROUTE_BUTTON, "dgis://2gis.ru/routeSearch/rsType/car/to/44.8,41.7")
    markup = inline_keyboard([[UrlButton(ROUTE_BUTTON, EXPECTED)], [InlineButton("✅ Да", "ap:a:1")]])
    assert markup["inline_keyboard"][0] == [{"text": ROUTE_BUTTON, "url": EXPECTED}]
    signed = CallbackSigner("123456:test-bot-token-abcdefghijklmnop").sign_markup(markup)
    assert signed is not None
    assert signed["inline_keyboard"][0] == [{"text": ROUTE_BUTTON, "url": EXPECTED}]
    assert signed["inline_keyboard"][1][0]["callback_data"] != "ap:a:1"  # callbacks are still signed


def test_mac_app_gets_only_callback_buttons() -> None:
    markup = inline_keyboard([[UrlButton(ROUTE_BUTTON, EXPECTED)], [InlineButton("✅ Да", "ap:a:1")]])
    assert _buttons(markup) == [[{"text": "✅ Да", "data": "ap:a:1"}]]
    assert _buttons(inline_keyboard([[UrlButton(ROUTE_BUTTON, EXPECTED)]])) == []


# --- on «когда выходить?» and on reminders ----------------------------------------------------------------------


async def _appointment(c: Any, worker: Worker, start: datetime, location: str = DEST) -> None:
    c.routes.durations[(c.settings.default_origin, location)] = 22 * 60
    c.calendar.add_event(
        "primary",
        summary="Встреча",
        location=location,
        start={"dateTime": iso(start)},
        end={"dateTime": iso(start + timedelta(hours=1))},
    )
    async with c.db.session() as s:
        await enqueue(s, job_type="calendar.reconcile", run_at=c.clock.now())  # also plans the reminders
    await worker.drain()


def known_points(c: Any) -> None:
    c.geocoder.points[c.settings.default_origin] = HOME
    c.geocoder.points[DEST] = DEST_POINT


@pytest.mark.db
async def test_leave_reply_has_the_route_button(container: Any, worker: Worker) -> None:
    c = container
    known_points(c)
    await _appointment(c, worker, local(10, 30))
    await BotService(c).handle_update(text_msg("/leave", 1))
    last = c.telegram.sent[-1]
    assert last["text"] == "🚶 Выходить на «Встреча» в 09:58 — через 58 мин (дорога ~22 мин от дома + запас 10 мин)."
    assert url_buttons(last["reply_markup"]) == [{"text": ROUTE_BUTTON, "url": EXPECTED}]


@pytest.mark.db
async def test_leave_reply_from_a_shared_location(container: Any, worker: Worker) -> None:
    c = container
    known_points(c)
    here = format_coordinates(41.7483, 44.8104)
    c.routes.durations[(here, DEST)] = 10 * 60
    await _appointment(c, worker, local(10, 30))
    bot = BotService(c)
    location = {"latitude": 41.7483, "longitude": 44.8104}
    message = {"message_id": 5, "from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "location": location}
    message["date"] = int(c.clock.now().timestamp())
    await bot.handle_update({"update_id": 1, "message": message})
    await bot.handle_update(text_msg("когда выходить?", 2))
    [button] = url_buttons(c.telegram.sent[-1]["reply_markup"])
    assert button["url"].startswith("https://2gis.ge/tbilisi/directions/points/44.810400%2C41.748300%7C")
    # the route starts at the shared point (above); home is geocoded once — for «до дома» in the location reply
    assert c.geocoder.calls.count(c.settings.default_origin) == 1


@pytest.mark.db
async def test_no_button_without_coordinates(container: Any, worker: Worker) -> None:
    c = container
    c.geocoder.points[c.settings.default_origin] = HOME  # the destination is unknown to the geocoder
    await _appointment(c, worker, local(10, 30))
    bot = BotService(c)
    await bot.handle_update(text_msg("/leave", 1))
    assert c.telegram.sent[-1]["text"].startswith("🚶 Выходить на «Встреча»")
    assert url_buttons(c.telegram.sent[-1]["reply_markup"]) == []
    c.travel.geocoder = None  # no OPENROUTESERVICE_API_KEY at all
    await bot.handle_update(text_msg("/leave", 2))
    assert url_buttons(c.telegram.sent[-1]["reply_markup"]) == []


@pytest.mark.db
async def test_no_button_when_there_is_no_meeting(container: Any) -> None:
    c = container
    known_points(c)
    await BotService(c).handle_update(text_msg("/leave", 1))
    assert c.telegram.sent[-1]["text"] == "Ближайших встреч нет."
    assert c.telegram.sent[-1]["reply_markup"] is None


@pytest.mark.db
async def test_departure_reminders_carry_the_route_button(container: Any, worker: Worker, clock: FrozenClock) -> None:
    c = container
    known_points(c)
    await _appointment(c, worker, local(15, 0))  # leave at 14:28
    clock.set(local(13, 58))
    await worker.drain()
    reminder = c.telegram.sent[-1]
    assert "Через 30 мин нужно выходить" in reminder["text"]
    assert url_buttons(reminder["reply_markup"]) == [{"text": ROUTE_BUTTON, "url": EXPECTED}]


@pytest.mark.db
async def test_reminder_without_coordinates_goes_out_without_a_button(
    container: Any, worker: Worker, clock: FrozenClock
) -> None:
    c = container
    await _appointment(c, worker, local(15, 0))  # the geocoder knows neither address
    clock.set(local(13, 58))
    await worker.drain()
    assert "Через 30 мин нужно выходить" in c.telegram.sent[-1]["text"]
    assert not url_buttons(
        c.telegram.sent[-1].get("reply_markup")
    )  # only «👌 Понял · ⏰ 10 мин» (persistent reminders), no route


@pytest.mark.db
async def test_task_reminders_have_no_route_button(container: Any, worker: Worker, clock: FrozenClock) -> None:
    c = container
    known_points(c)
    c.todoist.add(id="CALL", content="Позвонить маме", due={"date": "2026-09-23", "datetime": iso(local(12, 0))})
    async with c.db.session() as s:
        await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now())
    await worker.drain()
    async with c.db.session() as s:
        await enqueue(s, job_type="reminders.plan", run_at=c.clock.now())
    await worker.drain()
    clock.set(local(12, 0))
    await worker.drain()
    assert c.telegram.sent[-1]["text"].startswith("🔔 Напоминание: «Позвонить маме»")
    assert not url_buttons(c.telegram.sent[-1].get("reply_markup"))


def test_fake_telegram_keeps_url_buttons() -> None:
    """The signing fake (like the real adapter) passes URL buttons through untouched."""
    fake = FakeTelegramApi(signer=CallbackSigner("123456:test-bot-token-abcdefghijklmnop"))
    markup = inline_keyboard([[UrlButton(ROUTE_BUTTON, EXPECTED)]])
    assert fake.signer is not None and fake.signer.sign_markup(markup) == markup


class OrsLikeRoutes(FakeRoutesApi):
    label: ClassVar[str] = "OpenRouteService"
    budget_key: ClassVar[str] = "openrouteservice"
    needs_coordinates: ClassVar[bool] = True


@pytest.mark.db
async def test_leave_names_the_active_provider(container: Any, worker: Worker) -> None:
    c = container
    c.travel.routes = OrsLikeRoutes()
    c.geocoder.points[c.settings.default_origin] = HOME  # the meeting's address is not found
    await _appointment(c, worker, local(10, 30))
    await BotService(c).handle_update(text_msg("/leave", 1))
    assert c.telegram.sent[-1]["text"] == (
        f"«Встреча» в 10:30, 📍 {DEST}.\n"
        "OpenRouteService не нашёл дорогу до этого адреса. Я не буду угадывать — проверь дорогу сам."
    )
    assert c.telegram.sent[-1]["reply_markup"] is None  # no coordinates for the meeting → no 2GIS button


# --- the reply to a shared location: where you are, how far home, where to go ------------------------------------


@pytest.mark.db
async def test_location_reply_says_how_far_home_and_where_to_go(container: Any, worker: Worker) -> None:
    """The user's feedback 2026-10-14: a location reply that only says «Принял» is useless — say how far home and
    which events / errands mean going somewhere, with 2GIS buttons."""
    c = container
    known_points(c)
    here = format_coordinates(41.7483, 44.8104)
    c.routes.durations[(here, c.settings.default_origin)] = 12 * 60  # back home
    c.routes.durations[(here, DEST)] = 10 * 60
    await _appointment(c, worker, local(10, 30))
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-23"})
    c.todoist.add(id="CODE", content="Разобраться в ИИ-агентах")  # not an errand
    async with c.db.session() as s:
        await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now())
    await worker.drain()
    bot = BotService(c)
    message = {"message_id": 7, "from": {"id": USER}, "chat": {"id": USER, "type": "private"},
               "location": {"latitude": 41.7483, "longitude": 44.8104},
               "date": int(c.clock.now().timestamp())}  # fmt: skip
    await bot.handle_update({"update_id": 10, "message": message})
    text = c.telegram.texts()[-1]
    assert text.startswith("📍 Принял, считаю дорогу от этой точки до 11:00.")
    assert "🏠 До дома ~12 мин на машине" in text
    assert "🚗 Куда ехать\n• 10:30 Встреча — ~10 мин от вашей геопозиции, выходить в 10:10" in text
    assert "🏃 Дела вне дома\n• Постричься — 23.09" in text and "ИИ-агентах" not in text
    markup = c.telegram.sent[-1]["reply_markup"]
    labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert labels == ["🧭 Домой в 2ГИС", "🧭 К «Встреча» в 2ГИС", "🏠 Я дома"]
    home_url = url_buttons(markup)[0]["url"]
    assert home_url.startswith(
        "https://2gis.ge/tbilisi/directions/points/44.810400%2C41.748300%7C44.830400%2C41.744300"
    )


@pytest.mark.db
async def test_location_next_to_home_says_you_are_home(container: Any) -> None:
    c = container
    known_points(c)
    bot = BotService(c)
    message = {"message_id": 8, "from": {"id": USER}, "chat": {"id": USER, "type": "private"},
               "location": {"latitude": HOME.latitude + 0.0005, "longitude": HOME.longitude},
               "date": int(c.clock.now().timestamp())}  # fmt: skip
    await bot.handle_update({"update_id": 11, "message": message})
    text = c.telegram.texts()[-1]
    assert "🏠 Вы дома." in text and "До дома" not in text
    assert "Встреч с адресом и дел вне дома на сегодня нет." in text
