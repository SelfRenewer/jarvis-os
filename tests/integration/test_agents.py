"""The four chat agents: agenda, task management, morning/evening briefings, AI Inbox (fakes + real PostgreSQL).

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi (05:00 UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from jarvis.bot.intents import Intent, RoutedIntent, keyword_route
from jarvis.bot.service import BotService
from jarvis.db.models import Approval, Note, TodoistTask
from jarvis.integrations.llm.client import strict_json_schema
from jarvis.jobs.handlers import periodic_tasks
from jarvis.scheduler.queue import enqueue
from tests.conftest import make_settings

pytestmark = pytest.mark.db
USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


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


def tap(data: str, update_id: int, callback_id: str | None = None) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id or f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }


def buttons(sent: dict[str, Any]) -> list[tuple[str, str]]:
    markup = sent.get("reply_markup") or {}
    return [(b["text"], b["callback_data"]) for row in markup.get("inline_keyboard", []) for b in row]


async def sync(c: Any) -> None:
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    async with c.db.session() as s:
        await c.calendar_sync.reconcile(s, c.clock.now())


def event(c: Any, title: str, start: datetime, end: datetime, **extra: Any) -> None:
    c.calendar.add_event("primary", summary=title, start={"dateTime": iso(start)}, end={"dateTime": iso(end)}, **extra)


def timed(day: int, hour: int, minute: int = 0) -> dict[str, Any]:
    return {"date": f"2026-09-{day:02d}", "datetime": iso(local(day, hour, minute)), "is_recurring": False}


# --- 1. Agenda ---------------------------------------------------------------------------------------------


async def seed_week(c: Any) -> None:
    event(c, "Снять 2 000 $", local(23, 13), local(23, 14))
    event(c, "Врач", local(24, 10), local(24, 11), location="Rustaveli 5")
    c.todoist.add(id="CASH", content="Снять 2 000 $", due=timed(23, 13))  # the same item as the event
    c.todoist.add(id="HAIR", content="Постричься", priority=4, due={"date": "2026-09-24"})
    c.todoist.add(id="CIGAR", content="Купить сигару", due={"date": "2026-09-22"})  # overdue
    c.todoist.add(id="REP", content="Отчёт", due={"date": "2026-09-26"}, deadline={"date": "2026-09-27"})
    await sync(c)


async def test_agenda_tomorrow_week_and_errors(container: Any) -> None:
    c = container
    await seed_week(c)
    bot = BotService(c)

    await bot.handle_update(msg("Что у меня завтра?", 1))
    tomorrow = c.telegram.texts()[-1]
    assert tomorrow.startswith("📅 Завтра, чт 24.09")
    assert "• 10:00–11:00 Врач 📍Rustaveli 5" in tomorrow
    assert "☐ 🔴 Постричься" in tomorrow
    assert "Купить сигару" not in tomorrow  # overdue items belong to today

    await bot.handle_update(msg("что на неделе?", 2))
    week = c.telegram.texts()[-1]
    assert week.startswith("🗓 23.09 – 29.09")
    assert week.count("Снять 2 000 $") == 1  # task + calendar event shown once
    assert "⚠️ Просрочено (1):" in week
    assert "☐ 22.09 Купить сигару" in week
    assert "📆 Сб 26.09" in week
    assert "☐ Отчёт ⏳ дедлайн 27.09" in week
    assert "📆 Вт 29.09 — свободно" in week
    assert "Итого: 2 события" in week

    await bot.handle_update(msg("/agenda 20.09", 3))
    assert "уже прошло" in c.telegram.texts()[-1]
    await bot.handle_update(msg("/agenda когда-нибудь потом", 4))
    assert c.telegram.texts()[-1].startswith("Не понял дату")
    await bot.handle_update(msg("/agenda с 01.10 по 10.10", 5))
    assert "Календарь я вижу до 07.10" in c.telegram.texts()[-1]
    await bot.handle_update(msg("/agenda", 6))
    assert c.telegram.texts()[-1].startswith("🗓 23.09 – 29.09")


async def test_today_uses_agenda_and_dedupes_near_duplicates(container: Any) -> None:
    """R11 in /today: «Уточнить у владельца квартиры вызов мастера» == event «Уточнить у владельца вызов мастера»."""
    c = container
    event(c, "Уточнить у владельца вызов мастера", local(23, 10), local(23, 10, 15))
    c.todoist.add(id="OWN", content="Уточнить у владельца квартиры вызов мастера", due=timed(23, 10))
    c.todoist.add(id="PILL", content="Купить пластыри", due={"date": "2026-09-23"})
    await sync(c)
    await BotService(c).handle_update(msg("/today", 10))
    text = c.telegram.texts()[-1]
    assert text.startswith("📅 Сегодня, ср 23.09")
    assert "квартиры" not in text
    assert "☐ Купить пластыри" in text


# --- 2. Task management -------------------------------------------------------------------------------------


async def test_complete_by_name_with_undo(container: Any) -> None:
    c = container
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-24"})
    c.todoist.add(id="MILK", content="Купить молоко", due={"date": "2026-09-23"})
    await sync(c)
    bot = BotService(c)
    await bot.handle_update(msg("готово: стрижка", 20))
    assert ("close", "HAIR") in c.todoist.calls
    assert ("close", "MILK") not in c.todoist.calls
    assert c.telegram.texts()[-1] == "✅ Готово: «Постричься»"
    assert buttons(c.telegram.sent[-1]) == [("↩️ Вернуть", "tr:HAIR")]
    async with c.db.session() as s:
        assert (await s.get(TodoistTask, "HAIR")).checked  # type: ignore[union-attr]
    await bot.handle_update(tap("tr:HAIR", 21))
    assert ("reopen", "HAIR") in c.todoist.calls
    assert "Вернул «Постричься»" in c.telegram.texts()[-1]
    await bot.handle_update(msg("готово", 22))
    assert "Что именно готово?" in c.telegram.texts()[-1]


async def test_ambiguous_name_asks_with_buttons_once(container: Any) -> None:
    c = container
    c.todoist.add(id="M", content="Купить молоко", due={"date": "2026-09-23"})
    c.todoist.add(id="B", content="Купить хлеб", due={"date": "2026-09-23"})
    await sync(c)
    bot = BotService(c)
    await bot.handle_update(msg("готово: купить", 30))
    assert not [call for call in c.todoist.calls if call[0] == "close"]  # never guesses
    options = buttons(c.telegram.sent[-1])
    assert [text for text, _ in options] == ["Купить молоко", "Купить хлеб", "✖️ Отмена"]
    await bot.handle_update(tap(options[1][1], 31))
    await bot.handle_update(tap(options[1][1], 32))  # double tap
    assert [call for call in c.todoist.calls if call[0] == "close"] == [("close", "B")]
    assert c.telegram.answered[-1] == ("cb32", "Уже обработано")


async def test_delete_needs_confirmation(container: Any, worker: Any) -> None:
    c = container
    c.todoist.add(id="CIG", content="Купить сигару")
    c.todoist.add(id="BAL", content="Продумать балкон")
    await sync(c)
    bot = BotService(c)
    await bot.handle_update(msg("удали задачу купить сигару", 40))
    assert c.telegram.texts()[-1] == "🗑 Удалить задачу «Купить сигару»? Это нельзя отменить."
    assert not [call for call in c.todoist.calls if call[0] == "delete"]
    approve = buttons(c.telegram.sent[-1])[0][1]
    await bot.handle_update(tap(approve, 41))
    await worker.drain()
    assert ("delete", "CIG") in c.todoist.calls
    assert c.telegram.texts()[-1].startswith("🗑 Удалено: 1")

    await bot.handle_update(msg("удали балкон", 42))
    reject = buttons(c.telegram.sent[-1])[1][1]
    await bot.handle_update(tap(reject, 43))
    await worker.drain()
    assert ("delete", "BAL") not in c.todoist.calls
    assert c.telegram.texts()[-1] == "Отменено — задачи не трогаю."


async def test_priority_and_reschedule_keep_time(container: Any) -> None:
    c = container
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-24"})
    c.todoist.add(id="FLW", content="Заказать цветы", due=timed(23, 16), duration={"amount": 30, "unit": "minute"})
    c.todoist.add(
        id="GYM", content="Зарядка", due={"date": "2026-09-23", "is_recurring": True, "string": "каждый день"}
    )
    await sync(c)
    bot = BotService(c)
    await bot.handle_update(msg("приоритет 1: стрижка", 50))
    assert ("update", ("HAIR", {"priority": 4})) in c.todoist.calls  # p1 = API priority 4
    assert c.telegram.texts()[-1] == "🔴 Приоритет 1: «Постричься»"

    await bot.handle_update(msg("перенеси цветы на пятницу", 51))
    expected = {"due_datetime": "2026-09-25T12:00:00Z", "duration": 30, "duration_unit": "minute"}
    assert ("update", ("FLW", expected)) in c.todoist.calls  # 16:00 local kept
    assert c.telegram.texts()[-1] == "🗓 «Заказать цветы» → пт 25.09 16:00"

    await bot.handle_update(msg("перенеси стрижку на 18:30", 52))
    assert ("update", ("HAIR", {"due_datetime": iso(local(24, 18, 30))})) in c.todoist.calls

    await bot.handle_update(msg("перенеси стрижку на 20.09", 53))
    assert "уже прошло" in c.telegram.texts()[-1]
    await bot.handle_update(msg("перенеси зарядку на завтра", 54))
    assert "повторяющаяся" in c.telegram.texts()[-1]


async def test_bulk_reschedule_overdue_after_confirmation(container: Any, worker: Any) -> None:
    c = container
    c.todoist.add(id="A", content="Позвонить в банк", due=timed(22, 10))
    c.todoist.add(id="B", content="Купить батарейки", due={"date": "2026-09-21"})
    c.todoist.add(id="R", content="Зарядка", due={"date": "2026-09-22", "is_recurring": True, "string": "every day"})
    c.todoist.add(id="T", content="Сегодняшняя", due={"date": "2026-09-23"})
    await sync(c)
    bot = BotService(c)
    await bot.handle_update(msg("перенеси все просроченные на завтра", 60))
    text = c.telegram.texts()[-1]
    assert text.startswith("➡️ Перенести (2)?")
    assert "• Позвонить в банк → завтра, чт 24.09 10:00" in text
    assert "• Купить батарейки → завтра, чт 24.09" in text
    assert "Повторяющиеся не переношу: Зарядка" in text
    assert not [call for call in c.todoist.calls if call[0] == "update"]  # nothing before the button
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])[0][1], 61))
    await worker.drain()
    updates = [call for call in c.todoist.calls if call[0] == "update"]
    assert sorted(updates) == [
        ("update", ("A", {"due_datetime": iso(local(24, 10))})),
        ("update", ("B", {"due_date": "2026-09-24"})),
    ]
    assert c.telegram.texts()[-1].startswith("➡️ Перенесено: 2")

    # A retried job never repeats work (tasks are already at their targets).
    async with c.db.session() as s:
        approval = await s.scalar(select(Approval).where(Approval.kind == "task_op"))
        assert approval is not None
        again = await c.task_ops.apply_approved(s, c.todoist, c.todoist_sync, approval.id, c.clock.now())
    assert (again["done"], again["already"]) == (0, 2)
    assert len([call for call in c.todoist.calls if call[0] == "update"]) == 2


async def test_bulk_complete_recurring_is_idempotent(container: Any, worker: Any) -> None:
    """Completing a recurring task advances it; a retried job must not advance it a second time."""
    c = container
    c.todoist.add(id="R", content="Зарядка", due={"date": "2026-09-23", "is_recurring": True, "string": "every day"})
    await sync(c)
    bot = BotService(c)
    await bot.handle_update(msg("отметь все сегодняшние выполненными", 70))
    assert c.telegram.texts()[-1].startswith("✅ Отметить выполненными (1)?")
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])[0][1], 71))
    await worker.drain()
    async with c.db.session() as s:
        approval = await s.scalar(select(Approval).where(Approval.kind == "task_op"))
        assert approval is not None
        again = await c.task_ops.apply_approved(s, c.todoist, c.todoist_sync, approval.id, c.clock.now())
    assert c.todoist.calls.count(("close", "R")) == 1
    assert again["already"] == 1


# --- 3. Briefings -------------------------------------------------------------------------------------------


def test_periodic_schedule() -> None:
    names = {t.name for t in periodic_tasks(make_settings())}
    assert {"briefing.morning", "briefing.evening"} <= names
    assert "planner.daily" not in names  # the morning briefing carries the plan
    names_off = {t.name for t in periodic_tasks(make_settings(briefing_morning_enabled=False))}
    assert "planner.daily" in names_off
    assert "briefing.morning" not in names_off


async def test_morning_briefing_includes_plan(container: Any, worker: Any) -> None:
    c = container
    await seed_week(c)
    async with c.db.session() as s:
        await enqueue(s, job_type="briefing.morning", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()
    assert len(c.telegram.sent) == 1
    text = c.telegram.texts()[0]
    assert text.startswith("☀️ Доброе утро! Сегодня среда, 23.09.")
    assert "• 13:00–14:00 Снять 2 000 $" in text
    assert "⚠️ Просрочено (1):" in text
    assert "📆 Завтра: 1 событие, 1 задача." in text
    assert "🗓 Предложение по расписанию\n" in text  # the overdue date-only task is planned for today
    assert "<b>Предложение по расписанию</b>" in c.telegram.sent[0]["text"]  # the plan's title is bold
    assert buttons(c.telegram.sent[0])[0][1].startswith("ap:a:")


async def test_stale_briefing_is_skipped(container: Any, worker: Any) -> None:
    c = container
    c.clock.set(local(23, 13))  # 5 h after 08:00 (grace 3 h): Mac was asleep
    async with c.db.session() as s:
        await enqueue(s, job_type="briefing.morning", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()
    assert c.telegram.sent == []


async def test_evening_review_move_by_words_and_complete_by_button(container: Any, worker: Any) -> None:
    c = container
    c.clock.set(local(23, 21))
    event(c, "Снять 2 000 $", local(23, 13), local(23, 14))
    c.todoist.add(id="CASH", content="Снять 2 000 $", due=timed(23, 13))  # probably done, just not ticked
    c.todoist.add(id="PILL", content="Купить батарейки", due={"date": "2026-09-23"})
    c.todoist.add(id="LATE", content="Позвонить", due=timed(23, 22))  # still ahead tonight
    c.todoist.add(id="GYM", content="Зарядка", due={"date": "2026-09-23", "is_recurring": True, "string": "daily"})
    async with c.db.session() as s:
        await enqueue(s, job_type="briefing.evening", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()  # the job refreshes Todoist/Calendar itself
    # the review, then the check-in (today's TLT has no score or mood): score buttons first
    assert c.telegram.texts()[-1].splitlines()[:2] == [
        "🌙 Как прошёл день? Ответьте кнопкой или словами — «пропусти», «готово», «стоп».",
        "⭐ Оценка дня от 0 до 10? · шаг 1 из 2",
    ]
    text = c.telegram.texts()[-2]
    assert text.startswith("🌙 Итоги дня · среда 23.09")
    assert "⏳ Не сделано (1):\n☐ Купить батарейки" in text
    assert "возможно, уже сделано (1):\n☐ 13:00 Снять 2 000 $" in text
    assert "Позвонить" not in text
    assert "Повторяющиеся (не переношу, только отметить): Зарядка" in text
    assert "➡️ Перенести — скажите: «перенеси всё невыполненное на завтра»" in text
    labels = buttons(c.telegram.sent[-2])
    # moving is said, not tapped (2026-10-01); score and mood come as the check-in right after
    assert [label for label, _ in labels] == ["✅ Отметить сделанными (1)"]
    bot = BotService(c)
    await bot.handle_update(msg("перенеси батарейки на завтра", 80))
    await bot.handle_update(tap(labels[0][1], 81))
    await worker.drain()
    assert ("update", ("PILL", {"due_date": "2026-09-24"})) in c.todoist.calls
    assert ("close", "CASH") in c.todoist.calls
    assert ("close", "GYM") not in c.todoist.calls


# --- 4. AI Inbox --------------------------------------------------------------------------------------------


def llm_says(c: Any, *actions: dict[str, Any], **fields: Any) -> None:
    """Script the router: one action (keyword arguments) or several (dicts), as the LLM returns them."""
    items = [RoutedIntent(**a).model_dump(mode="json") for a in actions] or [
        RoutedIntent(**fields).model_dump(mode="json")
    ]
    c.llm.responders["intent_router"] = lambda _user, _messages: {"actions": items}


async def test_inbox_files_confident_messages_automatically(container: Any) -> None:
    c = container
    bot = BotService(c)
    llm_says(c, intent=Intent.ADD_TASK, task_text="Позвонить маме", due_string="завтра", confidence="high")
    await bot.handle_update(msg("позвонить маме завтра", 90))
    assert ("create", ("Позвонить маме", "завтра")) in c.todoist.calls
    # 16.10: the LLM never files a note by itself — a remark it took for «information» gets a hint
    llm_says(c, intent=Intent.CAPTURE_NOTE, note_text="где-то гигабайт оперативки ест", confidence="high")
    await bot.handle_update(msg("где-то гигабайт оперативки ест ладно", 91))
    assert c.telegram.texts()[-1] == "Понял. Чтобы сохранить это, скажите «Заметка: …»."
    async with c.db.session() as s:
        assert (await s.scalars(select(Note))).all() == []
    await bot.handle_update(msg("Заметка: код домофона 4521", 92))  # an explicit command saves at once
    assert c.telegram.texts()[-1] == "📝 Заметка сохранена.\nкод домофона 4521"
    await bot.handle_update(msg("/notes домофон", 93))
    assert "код домофона 4521" in c.telegram.texts()[-1]


async def test_inbox_asks_when_unsure_and_resolves_once(container: Any) -> None:
    c = container
    bot = BotService(c)
    llm_says(c, intent=Intent.SHOPPING_ADD, shopping_text="велосипед", confidence="low")
    await bot.handle_update(msg("велосипед", 100))
    assert c.telegram.texts()[-1] == "📥 Куда записать «велосипед»?\nПохоже на: покупка."
    options = dict((label, data) for label, data in buttons(c.telegram.sent[-1]))
    assert set(options) == {"✅ Задача", "💡 Идея", "🛒 Покупка", "📝 Заметка", "✖️ Ничего"}
    await bot.handle_update(tap(options["📝 Заметка"], 101))
    await bot.handle_update(tap(options["✅ Задача"], 102))  # a second tap is ignored
    async with c.db.session() as s:
        notes = (await s.scalars(select(Note))).all()
    assert [n.text for n in notes] == ["велосипед"]
    assert not [call for call in c.todoist.calls if call[0] == "create"]


async def test_inbox_without_llm_asks_and_creates_task(container: Any) -> None:
    c = container
    bot = BotService(c)  # the fake LLM has no router responder → behaves like an LLM outage
    await bot.handle_update(msg("Подарок маме в пятницу", 110))
    options = dict(buttons(c.telegram.sent[-1]))
    await bot.handle_update(tap(options["✅ Задача"], 111))
    assert ("create", ("Подарок маме", "в пятницу")) in c.todoist.calls
    await bot.handle_update(msg("Купить подарок маме в пятницу", 114))  # a purchase with a date: a task, no question
    assert ("create", ("Купить подарок маме", "в пятницу")) in c.todoist.calls
    await bot.handle_update(msg("сколько стоит ремонт балкона?", 112))
    assert c.telegram.texts()[-1].startswith("Не понял запрос")  # questions are not filed
    await bot.handle_update(msg("сколько стоит биткоин?", 115))
    assert c.telegram.texts()[-1].startswith("💱 1 BTC ≈")  # since 2026-10-13 answered by code (R26)
    await bot.handle_update(msg("спасибо", 113))
    assert c.telegram.texts()[-1] == "Всегда рад помочь."  # small talk is answered, not filed


async def test_llm_unknown_is_not_filed(container: Any) -> None:
    c = container
    llm_says(c, intent=Intent.UNKNOWN)
    await BotService(c).handle_update(msg("как настроение?", 120))
    assert c.telegram.texts()[-1].startswith("Не понял запрос")
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Note)) == 0


# --- routing ------------------------------------------------------------------------------------------------


def test_router_schema_keeps_all_fields() -> None:
    schema = strict_json_schema(RoutedIntent)
    assert schema["required"] == list(RoutedIntent.model_fields)
    assert "_source" not in schema["properties"]


@pytest.mark.parametrize(
    ("text", "intent", "fields"),
    [
        ("Что у меня завтра?", Intent.SHOW_AGENDA, {"date_text": "завтра"}),
        ("какие планы на выходные", Intent.SHOW_AGENDA, {"date_text": "на выходные"}),
        ("покажи задачи на неделю", Intent.SHOW_AGENDA, {"date_text": "на неделю"}),
        ("Что у меня сегодня?", Intent.SHOW_TODAY, {}),
        ("Покажи мои задачи", Intent.SHOW_TASKS, {}),
        ("готово: стрижка", Intent.COMPLETE_TASK, {"task_text": "стрижка"}),
        ("отметь стрижку выполненной", Intent.COMPLETE_TASK, {"task_text": "стрижку"}),
        ("удали задачу купить сигару", Intent.DELETE_TASK, {"task_text": "купить сигару"}),
        ("сделай приоритет 1 у стрижки", Intent.SET_PRIORITY, {"priority": 1, "task_text": "стрижки"}),
        ("перенеси стрижку в пятницу в 18:00", Intent.SCHEDULE_REFERENCED_TASK, {"due_string": "в пятницу в 18:00"}),
        ("перенеси звонок на работу на завтра", Intent.SCHEDULE_REFERENCED_TASK, {"task_text": "звонок на работу"}),
        ("перенеси все просроченные на завтра", Intent.BULK_TASKS, {"scope": "overdue", "task_op": "reschedule"}),
        ("удали все просроченные", Intent.BULK_TASKS, {"scope": "overdue", "task_op": "delete"}),
        ("заметка: код домофона 1234", Intent.CAPTURE_NOTE, {"note_text": "код домофона 1234"}),
        ("найди заметку wifi", Intent.SHOW_NOTES, {"query": "wifi"}),
        ("добавь задачу отчёт с приоритетом 1", Intent.ADD_TASK, {}),
        ("что я говорил про AI inbox?", Intent.SEARCH_IDEAS, {"query": "AI inbox"}),
    ],
)
def test_keyword_router_agents(text: str, intent: Intent, fields: dict[str, Any]) -> None:
    routed = keyword_route(text)
    assert routed.intent == intent
    for key, value in fields.items():
        assert getattr(routed, key) == value


async def test_choice_expires(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("велосипед", 130))
    data = buttons(c.telegram.sent[-1])[0][1]
    c.clock.advance(timedelta(hours=25))
    await bot.handle_update(tap(data, 131))
    assert c.telegram.answered[-1] == ("cb131", "Уже обработано")
    assert not [call for call in c.todoist.calls if call[0] == "create"]


async def test_task_name_prefers_longer_stems(container: Any) -> None:
    """R16 (found live with Gemini): «я постригся» offered «Сделать ИИ-агента для постановки задач» too."""
    c = container
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-24"})
    c.todoist.add(id="AI", content="Сделать ИИ-агента для постановки задач и напоминаний")
    await sync(c)
    async with c.db.session() as s:
        assert [t.id for t in await c.task_ops.find(s, "постригся")] == ["HAIR"]
        assert [t.id for t in await c.task_ops.find(s, "стрижку")] == ["HAIR"]  # R13 still holds
        assert [t.id for t in await c.task_ops.find(s, "ИИ-агента")] == ["AI"]


async def test_inbox_task_title_is_capitalized(container: Any) -> None:
    c = container
    llm_says(c, intent=Intent.ADD_TASK, task_text="позвонить маме", due_string="завтра в 12", confidence="high")
    await BotService(c).handle_update(msg("позвонить маме завтра в 12", 140))
    assert ("create", ("Позвонить маме", "завтра в 12")) in c.todoist.calls


# --- several requests in one message (R19) -------------------------------------------------------------------


async def test_two_requests_in_one_message_without_llm(container: Any) -> None:
    """R19 (found live 2026-09-24): «Давай лучше, постричься перенесём. Добавим навести порядок в квартире» went to
    «Куда записать?» — one intent per message, spoken forms («перенесём», «добавим») not understood."""
    c = container
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-23"})
    await sync(c)
    bot = BotService(c)  # the fake LLM has no router responder: the deterministic path must handle it
    await bot.handle_update(msg("Давай лучше, постричься перенесём. Добавим навести порядок в квартире", 150))
    assert len(c.telegram.sent) == 1  # one answer for the whole message
    text = c.telegram.texts()[-1]
    assert "Куда записать" not in text
    assert "🗓 На когда перенести «Постричься»?" in text
    assert "✅ Добавил задачу: Навести порядок в квартире" in text
    assert ("create", ("Навести порядок в квартире", None)) in c.todoist.calls
    assert not [call for call in c.todoist.calls if call[0] == "update"]  # no date said → nothing guessed
    assert buttons(c.telegram.sent[-1]) == []  # the date is said in words, not picked from buttons (2026-10-01)
    await bot.handle_update(msg("давай на субботу", 151))
    assert ("update", ("HAIR", {"due_date": "2026-09-26"})) in c.todoist.calls
    assert c.telegram.texts()[-1] == "🗓 «Постричься» → сб 26.09"


async def test_several_requests_routed_by_llm(container: Any) -> None:
    c = container
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-23"})
    await sync(c)
    llm_says(
        c,
        {"intent": Intent.SCHEDULE_REFERENCED_TASK, "task_text": "стрижку", "due_string": "на пятницу"},
        {"intent": Intent.SHOPPING_ADD, "shopping_text": "молоко"},
    )
    await BotService(c).handle_update(msg("Думаю, стрижку лучше отложить до пятницы. А в покупки — молоко", 160))
    assert ("update", ("HAIR", {"due_date": "2026-09-25"})) in c.todoist.calls
    text = c.telegram.texts()[-1]
    assert "🗓 «Постричься» → пт 25.09" in text and "🛒 Добавил в покупки: Молоко" in text
    assert len(c.telegram.sent) == 1


async def test_two_new_tasks_in_one_message_are_both_created(container: Any) -> None:
    """Two tasks from one message must not share a Todoist X-Request-Id (Todoist would drop the second)."""
    c = container
    await BotService(c).handle_update(msg("добавь задачу купить хлеб и добавь позвонить маме", 170))
    creates = [call for call in c.todoist.calls if call[0] == "create"]
    assert creates == [("create", ("Купить хлеб", None)), ("create", ("Позвонить маме", None))]
    assert BotService._request_id(USER, {"message_id": 5}) != BotService._request_id(
        USER, {"message_id": None, "_origin_id": 5, "_part": 1}
    )


async def test_multi_sentence_idea_is_one_idea(container: Any) -> None:
    from jarvis.db.models import Idea

    c = container
    await BotService(c).handle_update(msg("Запиши идею: бот для заметок. Он сортирует мысли. И напоминает.", 180))
    async with c.db.session() as s:
        ideas = (await s.scalars(select(Idea))).all()
    assert [i.raw_input for i in ideas] == ["бот для заметок. Он сортирует мысли. И напоминает."]


def test_spoken_commands_and_splitting() -> None:
    from jarvis.bot.intents import normalize_command, split_requests

    assert normalize_command("Давай лучше, постричься перенесём") == "перенеси постричься"
    assert normalize_command("Добавим навести порядок") == "добавь навести порядок"
    assert normalize_command("Джарвис, удалим задачу сигара") == "удали задачу сигара"
    assert split_requests("перенеси стрижку на завтра и добавь купить хлеб") == [
        "перенеси стрижку на завтра",
        "добавь купить хлеб",
    ]
    assert split_requests("купи хлеб и молоко") == ["купи хлеб и молоко"]  # a list, not two commands
    # a second question after «и» is its own request (eval 14.10: the agenda was lost, only the weather answered)
    assert split_requests("что у меня завтра и какая будет погода") == ["что у меня завтра", "какая будет погода"]
    assert split_requests("напомни купить хлеб и что-то к чаю") == ["напомни купить хлеб и что-то к чаю"]
    assert split_requests("заметка: код 1234. Подъезд 2.") == ["заметка: код 1234. Подъезд 2."]
    assert keyword_route("перенеси звонок на работу").due_string is None  # no garbage date → the bot asks


async def test_llm_sees_open_task_titles_and_all_its_actions_run(container: Any) -> None:
    """R19: the LLM gets open task titles (no ids) to tell «move the existing task» from «add a new one», and every
    action it returns runs — also for a message without an explicit separator."""
    c = container
    c.todoist.add(id="BARB", content="Найти барбера", due={"date": "2026-09-23"})
    await sync(c)
    seen: list[str] = []

    def responder(user: str, _messages: Any) -> dict[str, Any]:
        seen.append(user)
        return {
            "actions": [
                RoutedIntent(
                    intent=Intent.SCHEDULE_REFERENCED_TASK, task_text="Найти барбера", due_string="в субботу"
                ).model_dump(mode="json"),
                RoutedIntent(intent=Intent.SHOPPING_ADD, shopping_text="молоко и хлеб").model_dump(mode="json"),
            ]
        }

    c.llm.responders["intent_router"] = responder
    await BotService(c).handle_update(msg("барбера лучше поискать в субботу, а в покупки закинь молоко и хлеб", 190))
    assert "<tasks>\n- Найти барбера\n</tasks>" in seen[0]
    assert ("update", ("BARB", {"due_date": "2026-09-26"})) in c.todoist.calls
    assert "🛒 Добавил в покупки:" in c.telegram.texts()[-1]


# --- subtasks ------------------------------------------------------------------------------------------------

FLAT_ITEMS = [
    "Купить органайзер для столовых приборов",
    "Купить влажные салфетки",
    "Купить кухонный нож",
    "Купить мусорные мешки",
    "Купить стол для балкона и барный стул",
    "Купить разделочные доски",
]


async def seed_apartment(c: Any) -> None:
    """The user's real structure (2026-09-24): /tasks listed these subtasks flat, their parent lost among them."""
    c.todoist.add(id="BARB", content="Найти барбера", due={"date": "2026-09-24"})
    c.todoist.add(id="FLAT", content="Обустройство квартиры")
    for i, title in enumerate(FLAT_ITEMS):
        c.todoist.add(id=f"F{i}", content=title, parent_id="FLAT")
    c.todoist.add(id="DESK", content="Определить рабочее место")
    c.todoist.add(id="BALC", content="Продумать удобную работу на балконе", parent_id="DESK")
    c.todoist.add(id="LAMP", content="Выбрать лампу", parent_id="BALC")  # a sub-subtask
    await sync(c)


async def test_tasks_show_the_hierarchy(container: Any) -> None:
    c = container
    await seed_apartment(c)
    await BotService(c).handle_update(msg("/tasks", 200))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[:3] == ["📋 Задачи · 11 открыто", "", "• Найти барбера — 24.09"]
    flat = lines.index("📂 Обустройство квартиры · осталось 6")
    assert lines[flat + 1 : flat + 7] == [f"   ◦ {t}" for t in sorted(FLAT_ITEMS)]
    desk = lines.index("📂 Определить рабочее место · осталось 2")
    assert lines[desk + 1 : desk + 3] == ["   ◦ Продумать удобную работу на балконе", "      ◦ Выбрать лампу"]
    assert "• Купить кухонный нож" not in lines  # subtasks are no longer listed as top-level tasks


async def test_whats_left_in_a_task(container: Any) -> None:
    c = container
    await seed_apartment(c)
    await BotService(c).handle_update(msg("Что осталось по обустройству квартиры?", 210))
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[:2] == ["📂 Обустройство квартиры · осталось 6", ""]
    assert "   ◦ Купить кухонный нож" in lines


async def test_add_a_subtask_from_chat(container: Any) -> None:
    c = container
    await seed_apartment(c)
    bot = BotService(c)
    await bot.handle_update(msg("Добавь в обустройство квартиры купить тарелки", 220))
    created = next(t for t in c.todoist.tasks.values() if t["content"] == "Купить тарелки")
    assert created["parent_id"] == "FLAT"
    assert c.telegram.texts()[-1] == "✅ Добавил подзадачу в «Обустройство квартиры»: Купить тарелки"
    await bot.handle_update(msg("добавь в понедельник позвонить маме", 221))  # a date, not a parent task
    other = next(t for t in c.todoist.tasks.values() if "позвонить маме" in t["content"].lower())
    assert other.get("parent_id") is None
    llm_says(c, intent=Intent.ADD_TASK, task_text="Купить шторы", parent_text="Обустройство квартиры")
    await bot.handle_update(msg("и шторы туда же, в квартиру", 222))
    curtains = next(t for t in c.todoist.tasks.values() if t["content"] == "Купить шторы")
    assert curtains["parent_id"] == "FLAT"


async def test_completing_or_deleting_a_parent_needs_confirmation(container: Any, worker: Any) -> None:
    """Todoist completes/deletes the subtasks together with the parent — so the bot asks first."""
    c = container
    await seed_apartment(c)
    bot = BotService(c)
    await bot.handle_update(msg("удали задачу обустройство квартиры", 230))
    assert c.telegram.texts()[-1] == "🗑 Удалить задачу «Обустройство квартиры» и 6 подзадач? Это нельзя отменить."
    await bot.handle_update(msg("готово: обустройство квартиры", 231))
    assert c.telegram.texts()[-1] == "✅ Закрыть «Обустройство квартиры» и 6 подзадач? Todoist закроет их вместе."
    assert not [call for call in c.todoist.calls if call[0] in ("close", "delete")]
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])[0][1], 232))
    await worker.drain()
    assert ("close", "FLAT") in c.todoist.calls
    async with c.db.session() as s:
        closed = (await s.scalars(select(TodoistTask).where(TodoistTask.parent_id == "FLAT"))).all()
    assert closed and all(t.checked for t in closed)  # the cache knows at once, not after the next poll
    await bot.handle_update(msg("готово: барбер", 233))  # a task without subtasks: done at once, as before
    assert ("close", "BARB") in c.todoist.calls
