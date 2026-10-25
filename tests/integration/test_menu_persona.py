"""Reply keyboard, /start summary, JARVIS's manner, proactive suggestions, search, small talk, weather.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.bot.intents import Intent, RoutedIntent
from jarvis.bot.persona import ACKS, QUERIES
from jarvis.bot.service import BotService
from jarvis.integrations.telegram.client import html_to_plain
from jarvis.integrations.weather.client import Forecast, HourForecast
from jarvis.scheduler.queue import enqueue
from jarvis.services.container import build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.db
USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=TZ)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


def tap(data: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }


def text_of(sent: dict[str, Any]) -> str:
    """A sent message as the user reads it (HTML tags dropped)."""
    return html_to_plain(sent["text"]) if sent.get("parse_mode") == "HTML" else str(sent["text"])


def buttons(sent: dict[str, Any]) -> list[tuple[str, str]]:
    markup = sent.get("reply_markup") or {}
    return [(b["text"], b["callback_data"]) for row in markup.get("inline_keyboard", []) for b in row]


def rain_at_eleven() -> Forecast:
    codes = [3, 3, 61, 61, 61, 61, 3, 3, 3, 3, 3, 3]
    temps = [22, 21, 17, 17, 16, 16, 18, 19, 19, 19, 18, 18]
    hours = tuple(
        HourForecast(NOW + timedelta(hours=i), temps[i], codes[i], 80 if codes[i] >= 51 else 5)
        for i in range(len(codes))
    )
    return Forecast(NOW, 22, 3, hours)


@pytest.fixture
def jarvis(db: Any, clock: Any) -> Any:
    """The container as the user runs it: manner, keyboard, weather (fake)."""
    c = build_container(make_settings(persona_enabled=True, menu_keyboard_enabled=True), db=db, clock=clock)
    c.weather_api.forecast_value = rain_at_eleven()
    return c


@pytest.fixture
def jarvis_worker(jarvis: Any, db: Any, clock: Any) -> Any:
    from jarvis.core.retry import RetryPolicy
    from jarvis.jobs.handlers import build_registry
    from jarvis.scheduler.queue import JobQueue
    from jarvis.scheduler.runner import Worker

    return Worker(JobQueue(db, clock, worker_id="w", retry=RetryPolicy(10, 60, 3, 0)), build_registry(), jarvis)


def answers(c: Any) -> list[str]:
    """Reply texts without the one-time «keyboard installed» message."""
    return [t for t in c.telegram.texts() if not t.startswith("⌨️")]


async def seed(c: Any) -> None:
    c.todoist.add(id="CIG", content="Купить сигару", due={"date": "2026-09-22"})  # overdue
    c.todoist.add(id="FLW", content="Заказать цветы", due={"date": "2026-09-23", "datetime": iso(NOW.replace(hour=10))})
    c.todoist.add(id="MIX", content="Разобраться в ИИ-агентах", priority=4)
    c.calendar.add_event(
        "primary",
        summary="Забрать из аэропорта",
        start={"dateTime": iso(NOW.replace(hour=18))},
        end={"dateTime": iso(NOW.replace(hour=19))},
    )
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
        await c.calendar_sync.reconcile(s, c.clock.now())


async def test_start_greets_summarises_and_installs_the_keyboard(jarvis: Any) -> None:
    c = jarvis
    await seed(c)
    async with c.db.session() as s:
        await c.notes.save(s, text_value="код домофона 4521", source="test")
    await BotService(c).handle_update(msg("/start", 1))
    [start] = c.telegram.sent  # the keyboard comes with /start: no separate keyboard message
    text = text_of(start)
    assert start["text"].startswith("👋 <b>Доброе утро, сэр.</b> JARVIS к вашим услугам.\n\n📋 <b>Задачи</b>")
    assert "Открыто: 3 · сегодня: 1 · просрочено: 1\nна неделе 0 · без даты 1 · закрыто за 7 дней 0" in text
    assert "Встреч: 1 · ближайшая в 18:00 — «Забрать из аэропорта»" in text
    assert "🗂 Идей 0 · заметок 1 · в покупках 0" in text
    assert "🌡 Тбилиси: сейчас +22°, ☁️ облачно" in text
    assert "💡 Требуют внимания: 2 — загляните в «📅 Сегодня»" in text
    keyboard = start["reply_markup"]["keyboard"]
    assert [[b["text"] for b in row] for row in keyboard] == [
        ["📅 Сегодня", "📋 Задачи", "📓 Дневник"],
        ["🛒 Покупки", "⋯ Ещё"],
    ]
    assert start["reply_markup"]["is_persistent"] is True


async def test_keyboard_appears_once_without_start(jarvis: Any) -> None:
    c = jarvis
    bot = BotService(c)
    await bot.handle_update(msg("что у меня завтра?", 2))
    assert "keyboard" in (c.telegram.sent[-1]["reply_markup"] or {})
    count = len(c.telegram.sent)
    await bot.handle_update(msg("что у меня завтра?", 3))
    assert len(c.telegram.sent) == count + 1  # just the answer: the keyboard was installed already


async def test_menu_buttons_and_settings(jarvis: Any) -> None:
    c = jarvis
    await seed(c)
    bot = BotService(c)
    await bot.handle_update(msg("🔥 Главное", 4))
    focus = next(m for m in c.telegram.sent if text_of(m).startswith("🔥 Главное, сэр"))
    assert "• «Купить сигару» — просрочена с 22.09" in text_of(focus)
    assert "🔴 Приоритет 1: «Разобраться в ИИ-агентах»" in text_of(focus)
    assert ("✅ Купить сигару", "sd:CIG") in buttons(focus)
    await bot.handle_update(msg("⚙️ Настройки", 5))
    settings = c.telegram.sent[-1]
    assert text_of(settings).startswith("⚙️ Настройки")
    assert ("🩺 Статус и синхронизация", "st:status") in buttons(settings)
    await bot.handle_update(msg("📅 Сегодня", 6))
    today = text_of(c.telegram.sent[-1])
    assert today.split("\n")[0] in {q.format(a="сэр") for q in QUERIES}  # «Разумеется, сэр.» …
    assert "📅 Сегодня, ср 23.09" in today


async def test_manner_on_actions_queries_and_errors(jarvis: Any) -> None:
    c = jarvis
    bot = BotService(c)
    await bot.handle_update(msg("Добавь задачу купить хлеб", 10))
    first, second = (
        c.telegram.texts()[-2].split("\n")[:2] if "⌨️" in answers(c)[-1] else (answers(c)[-1].split("\n")[:2])
    )
    assert first in {a.format(a="сэр") for a in ACKS}
    assert second == "✅ Добавил задачу: Купить хлеб"
    c.todoist.unavailable = True
    await bot.handle_update(msg("Добавь задачу купить молоко", 11))
    assert answers(c)[-1].startswith("Прошу прощения, сэр. ⚠️ Не получилось")


async def test_suggestion_buttons_complete_and_move_by_words(jarvis: Any) -> None:
    c = jarvis
    await seed(c)
    bot = BotService(c)
    await bot.handle_update(msg("/today", 20))
    today = next(m for m in c.telegram.sent if "📅 Сегодня" in text_of(m))
    lines = text_of(today).split("\n")
    header = lines.index("📅 Сегодня, ср 23.09")  # after the manner line («Разумеется, сэр.»)
    assert lines[header + 1] == (
        "🌡 Тбилиси: сейчас +22°, ☁️ облачно; до конца дня +16°…+22°; 🌧 дождь с 11:00; ☁️ облачно с 15:00"
    )
    assert "💡 Требует внимания" in lines
    assert "• «Заказать цветы» — в 10:00, через 60 мин" in lines
    assert not [data for _, data in buttons(today) if data.startswith("sm:")]  # no move buttons (2026-10-01)
    assert "➡️ Перенести — скажите, например: «стрижку на пятницу в 18» или «на 2 часа позже»." in lines
    await bot.handle_update(tap("sd:CIG", 21))
    assert ("close", "CIG") in c.todoist.calls
    assert answers(c)[-1].split("\n")[1] == "✅ Готово: «Купить сигару»"
    await bot.handle_update(tap("sd:CIG", 22))  # a second tap is harmless
    assert c.todoist.calls.count(("close", "CIG")) == 1
    await bot.handle_update(tap("sm:FLW", 23))  # a move button left in an old message asks in words
    assert answers(c)[-1].startswith("🗓 На когда перенести «Заказать цветы»?\nСкажите или напишите:")
    await bot.handle_update(msg("на 2 часа позже", 24))  # the task is at 10:00 → 12:00
    assert answers(c)[-1].endswith("🗓 «Заказать цветы» → сегодня, ср 23.09 12:00")


async def test_what_should_i_do_now(jarvis: Any) -> None:
    c = jarvis
    await seed(c)
    await BotService(c).handle_update(msg("Чем бы мне заняться сейчас?", 30))
    text = text_of(next(m for m in c.telegram.sent if "Предлагаю" in m["text"]))
    assert text.startswith("⏳ До «Забрать из аэропорта» в 18:00 свободно 9 ч.\n🎯 Предлагаю, сэр")
    assert "1. «Купить сигару» — просрочена с 22.09" in text
    assert "3. «Разобраться в ИИ-агентах» — важная" in text


async def test_find_across_everything(jarvis: Any) -> None:
    c = jarvis
    await seed(c)
    async with c.db.session() as s:
        await c.notes.save(s, text_value="Код домофона 4521, подъезд 2", source="test")
        await c.shopping.add_items(s, "кухонный нож", c.clock.now())
    bot = BotService(c)
    await bot.handle_update(msg("где у меня код домофона", 40))
    assert "📝 Заметки\n• Код домофона 4521, подъезд 2" in answers(c)[-1]
    await bot.handle_update(msg("найди аэропорт", 41))
    assert "📅 Календарь\n• 23.09 18:00 Забрать из аэропорта" in answers(c)[-1]
    await bot.handle_update(msg("найди нож", 42))
    assert "🛒 Покупки\n• кухонный нож" in answers(c)[-1]
    await bot.handle_update(msg("найди билеты в Рим", 43))
    assert answers(c)[-1].startswith("Прошу прощения, сэр, ничего не нашёл по «билеты в Рим»")


async def test_small_talk(jarvis: Any) -> None:
    c = jarvis
    await seed(c)
    bot = BotService(c)
    await bot.handle_update(msg("Как дела?", 50))
    answer = next(m for m in c.telegram.sent if m["text"].startswith("Всё в порядке"))["text"]
    assert answer == (
        "Всё в порядке, сэр, системы работают. У вас на сегодня 1 задача; просрочено 1; ближайшая встреча в 18:00 — "
        "«Забрать из аэропорта». На улице +22°, ☁️ облачно. Чем могу помочь?"
    )
    c.llm.responders["intent_router"] = lambda _u, _m: {
        "actions": [
            RoutedIntent(intent=Intent.SMALL_TALK, reply="Отличный вопрос, сэр. Столица Грузии — Тбилиси.").model_dump(
                mode="json"
            )
        ]
    }
    await bot.handle_update(msg("а какая столица у Грузии", 51))
    assert answers(c)[-1] == "Отличный вопрос, сэр. Столица Грузии — Тбилиси."
    prompt = c.llm.calls[-1][1]
    assert "<context>" in prompt and "Погода:" in prompt  # the LLM gets the facts it may use


async def test_weather_question_and_change_alert(jarvis: Any, jarvis_worker: Any) -> None:
    worker = jarvis_worker
    c = jarvis
    bot = BotService(c)
    await bot.handle_update(msg("Какая погода?", 60))
    weather = next(m for m in c.telegram.sent if "🌡 Тбилиси" in m["text"])["text"]
    assert "🌧 Сэр, в Тбилиси около 11:00 начнётся дождь (вероятность 80%), похолодает до +17°." in weather
    before = len(c.telegram.sent)
    for _ in range(2):  # the same change is announced once
        async with c.db.session() as s:
            await enqueue(s, job_type="weather.watch", run_at=c.clock.now())
        await worker.drain()
    alerts = answers(c)[before - 1 :]
    assert alerts == [
        "🌧 Сэр, в Тбилиси около 11:00 начнётся дождь (вероятность 80%), похолодает до +17°. Сейчас +22°, облачно."
    ]
    night = datetime(2026, 9, 23, 23, 45, tzinfo=TZ)
    c.clock.set(night)  # sleep hours: silent even though rain starts within the hour
    c.weather_api.forecast_value = Forecast(
        night,
        15,
        3,
        tuple(HourForecast(night + timedelta(minutes=15 + 60 * i), 14, 61, 90) for i in range(4)),
    )
    c.weather._cache = None
    assert c.weather.upcoming_change(c.weather_api.forecast_value, night) is not None
    async with c.db.session() as s:
        await enqueue(s, job_type="weather.watch", run_at=c.clock.now())
    await worker.drain()
    assert len(c.telegram.sent) == before + 1


async def test_morning_briefing_has_weather_and_suggestions(jarvis: Any, jarvis_worker: Any) -> None:
    worker = jarvis_worker
    c = jarvis
    await seed(c)
    async with c.db.session() as s:
        await enqueue(s, job_type="briefing.morning", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()
    [brief] = [m for m in c.telegram.sent if m["text"].startswith("☀️")]
    lines = text_of(brief).split("\n")
    assert lines[0] == "☀️ Доброе утро, сэр! Сегодня среда, 23.09."
    assert lines[1].startswith("🌡 Тбилиси: сейчас +22°")
    assert "💡 Требует внимания" in lines
    labels = [label for label, _ in buttons(brief)]
    assert labels[:2] == ["✅ Применить", "✖️ Отклонить"]  # the plan first
    assert "✅ Купить сигару" in labels


async def test_late_in_the_day_todays_tasks_are_suggested(jarvis: Any) -> None:
    c = jarvis
    c.todoist.add(id="BARB", content="Найти барбера", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
        assert await c.agenda.attention(s, c.clock.now()) == []  # 09:00: the day has just begun
    c.clock.set(datetime(2026, 9, 23, 17, 0, tzinfo=TZ))
    async with c.db.session() as s:
        [item] = await c.agenda.attention(s, c.clock.now())
    assert (item.task.id, item.note) == ("BARB", "сегодня, до конца дня")
