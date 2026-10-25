"""Medication / supplement reminders and usage statistics.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from jarvis.bot.service import BotService
from jarvis.db.models import Medication, MedicationIntake, UserPreference
from jarvis.meds.service import parse_times, split_dose, split_name_and_times
from jarvis.scheduler.queue import enqueue

USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")


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


def buttons(sent: dict[str, Any]) -> list[tuple[str, str]]:
    markup = sent.get("reply_markup") or {}
    return [(b["text"], b["callback_data"]) for row in markup.get("inline_keyboard", []) for b in row]


async def tick(c: Any, worker: Any) -> list[str]:
    before = len(c.telegram.sent)
    async with c.db.session() as s:
        await enqueue(s, job_type="meds.tick", run_at=c.clock.now())
    await worker.drain()
    return c.telegram.texts()[before:]


@pytest.mark.parametrize(
    ("phrase", "name", "dose", "times"),
    [
        ("омегу-3 1 капсулу в 9:00 и 21:00", "Омега-3", "1 капсулу", ["09:00", "21:00"]),
        ("магний 400 мг перед сном", "Магний", "400 мг", ["22:30"]),
        ("витамин D утром", "Витамин D", None, ["09:00"]),
        ("таблетку в 9 вечера", "Таблетка", None, ["21:00"]),
        ("цинк утром и вечером", "Цинк", None, ["09:00", "20:00"]),
        ("пробиотик каждый день в 8 и в 20", "Пробиотик", None, ["08:00", "20:00"]),
    ],
)
def test_parse_course(phrase: str, name: str, dose: str | None, times: list[str]) -> None:
    name_part, times_part = split_name_and_times(phrase)
    assert split_dose(name_part) == (name, dose)
    assert [t.strftime("%H:%M") for t in parse_times(times_part)] == times


@pytest.mark.db
async def test_add_from_chat_without_duplicates(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("Напоминай пить омегу-3 1 капсулу в 9:00 и 21:00", 1))
    assert c.telegram.texts()[-1].startswith("💊 Буду напоминать: Омега-3 — 1 капсулу · 09:00 и 21:00.")
    await bot.handle_update(msg("напоминай пить омегу-3 в 10:00", 2))  # the same course: new time, no duplicate
    assert c.telegram.texts()[-1].startswith("💊 Обновил время: Омега-3")
    await bot.handle_update(msg("напоминай пить магний", 3))
    assert c.telegram.texts()[-1].startswith("Во сколько напоминать про «Магний»?")
    async with c.db.session() as s:
        meds = (await s.scalars(select(Medication))).all()
    assert [(m.name, m.times) for m in meds] == [("Омега-3", ["10:00"])]


@pytest.mark.db
async def test_reminder_nudge_and_buttons(container: Any, worker: Any) -> None:
    c = container
    await BotService(c).handle_update(msg("напоминай пить омегу-3 в 9:00", 10))
    assert await tick(c, worker) == ["💊 Время принять: Омега-3 (09:00)."]
    labels = [label for label, _ in buttons(c.telegram.sent[-1])]
    assert labels == ["✅ Принял", "⏰ 30 мин", "✖️ Пропустить"]
    taken = buttons(c.telegram.sent[-1])[0][1]
    assert await tick(c, worker) == []  # not twice
    c.clock.advance(timedelta(minutes=46))
    assert await tick(c, worker) == ["💊 Напоминаю: Омега-3 (09:00) ещё не отмечен."]
    c.clock.advance(timedelta(minutes=60))
    assert await tick(c, worker) == []  # a single nudge only
    bot = BotService(c)
    await bot.handle_update(tap(taken, 11))
    # «Омега-3» is in the LMF dictionary → the intake also lands in TLT (since 2026-10-13)
    assert c.telegram.texts()[-1] == "✅ Отметил: Омега-3.\n📓 Записал в TLT · сегодня, ср 23.09: 💊 Омега-3"
    await bot.handle_update(tap(taken, 12))
    assert c.telegram.answered[-1] == ("cb12", "Уже отмечено")
    async with c.db.session() as s:
        [intake] = (await s.scalars(select(MedicationIntake))).all()
    assert intake.status == "taken"


@pytest.mark.db
async def test_snooze_reminds_again(container: Any, worker: Any) -> None:
    c = container
    await BotService(c).handle_update(msg("напоминай пить магний в 9:00", 20))
    await tick(c, worker)
    snooze = buttons(c.telegram.sent[-1])[1][1]
    await BotService(c).handle_update(tap(snooze, 21))
    assert c.telegram.texts()[-1] == "⏰ Напомню про Магний через 30 минут."
    c.clock.advance(timedelta(minutes=31))
    assert await tick(c, worker) == ["💊 Время принять: Магний (09:00)."]


@pytest.mark.db
async def test_late_intake_is_a_question_and_old_one_is_missed(container: Any, worker: Any) -> None:
    """Mac asleep through 09:00: at 12:00 Jarvis must not say «take it now» — it asks whether it was taken."""
    c = container
    await BotService(c).handle_update(msg("напоминай пить омегу-3 в 9:00", 30))
    c.clock.set(datetime(2026, 9, 23, 12, 0, tzinfo=TZ))
    assert await tick(c, worker) == ["⚠️ Приём «Омега-3» в 09:00 не отмечен. Уже приняли?"]
    assert [label for label, _ in buttons(c.telegram.sent[-1])] == ["✅ Уже принял", "✖️ Пропустил"]
    c.clock.set(datetime(2026, 9, 23, 15, 30, tzinfo=TZ))
    assert await tick(c, worker) == []
    async with c.db.session() as s:
        [intake] = (await s.scalars(select(MedicationIntake))).all()
    assert intake.status == "missed"


@pytest.mark.db
async def test_a_dose_slept_through_is_marked_missed(container: Any, worker: Any) -> None:
    """R37: more than 7 h late used to be skipped before the intake was even read — pending forever."""
    c = container
    await BotService(c).handle_update(msg("напоминай пить омегу-3 в 9:00", 33))
    assert await tick(c, worker) == ["💊 Время принять: Омега-3 (09:00)."]
    c.clock.set(datetime(2026, 9, 24, 17, 1, tzinfo=TZ))  # the Mac slept for a day and a half
    assert await tick(c, worker) == []
    async with c.db.session() as s:
        rows = (await s.scalars(select(MedicationIntake).order_by(MedicationIntake.due_at))).all()
    assert [(r.due_at.astimezone(TZ).strftime("%d %H:%M"), r.status) for r in rows] == [
        ("23 09:00", "missed"), ("24 09:00", "missed"),
    ]  # fmt: skip


@pytest.mark.db
async def test_a_new_schedule_has_no_intake_before_it(container: Any, worker: Any) -> None:
    c = container
    c.clock.set(datetime(2026, 9, 24, 0, 30, tzinfo=TZ))
    await BotService(c).handle_update(msg("напоминай пить магний в 23:00", 34))
    assert await tick(c, worker) == []  # no «yesterday 23:00 — taken?» for a course added after it
    async with c.db.session() as s:
        assert (await s.scalars(select(MedicationIntake))).all() == []


@pytest.mark.db
async def test_took_it_marks_the_closest_dose(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("напоминай пить омегу-3 в 9:00 и 21:00", 35))
    c.clock.set(datetime(2026, 9, 23, 20, 30, tzinfo=TZ))
    await bot.handle_update(msg("выпил омегу", 36))
    assert c.telegram.texts()[-1].startswith("✅ Отметил приём: Омега-3 (21:00).")


@pytest.mark.db
async def test_undelivered_reminders_are_sent_by_the_next_tick(container: Any, worker: Any) -> None:
    """M8 (review 17.10): no chat configured → the job «succeeded» and every step of the tick stayed marked sent."""
    from jarvis.core.errors import NotConfiguredError

    c = container
    bot = BotService(c)
    await bot.handle_update(msg("напоминай пить омегу-3 в 9:00", 37))
    await bot.handle_update(msg("напоминай пить магний в 9:00", 38))
    real_send = c.notifier.send

    async def nowhere(*args: Any, **kwargs: Any) -> Any:
        raise NotConfiguredError("telegram: no chat")

    c.notifier.send = nowhere
    assert await tick(c, worker) == []
    async with c.db.session() as s:
        assert [i.reminded_at for i in (await s.scalars(select(MedicationIntake))).all()] == [None, None]
    c.notifier.send = real_send
    assert sorted(await tick(c, worker)) == ["💊 Время принять: Магний (09:00).", "💊 Время принять: Омега-3 (09:00)."]


@pytest.mark.db
async def test_took_it_list_and_remove(container: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("напоминай пить омегу-3 в 9:00 и 21:00", 40))
    await bot.handle_update(msg("выпил омегу", 41))
    assert (
        c.telegram.texts()[-1] == "✅ Отметил приём: Омега-3 (09:00).\n📓 Записал в TLT · сегодня, ср 23.09: 💊 Омега-3"
    )
    await bot.handle_update(msg("💊 Лекарства", 42))  # an old keyboard button still works
    text = c.telegram.texts()[-1]
    assert "💊 Омега-3\n09:00 ✅, 21:00 · · за 7 дней 1/1" in text
    assert "<b>Омега-3</b>" in c.telegram.sent[-1]["text"]
    assert buttons(c.telegram.sent[-1]) == []  # no 🗑 per course any more: removing is said in words
    await bot.handle_update(msg("больше не напоминай про омегу", 43))
    assert c.telegram.texts()[-1] == "Больше не напоминать про «Омега-3»?"
    await bot.handle_update(tap(buttons(c.telegram.sent[-1])[0][1], 44))
    assert c.telegram.texts()[-1] == "🗑 Больше не напоминаю про «Омега-3»."
    async with c.db.session() as s:
        assert (await s.scalars(select(Medication.active))).all() == [False]


@pytest.mark.db
async def test_morning_briefing_lists_todays_intakes(container: Any, worker: Any) -> None:
    c = container
    async with c.db.session() as s:
        await c.meds.add(s, "Омега-3", None, [time(9, 0), time(21, 0)])
        await c.meds.add(s, "Магний", "400 мг", [time(22, 30)])
    async with c.db.session() as s:
        await enqueue(s, job_type="briefing.morning", run_at=c.clock.now(), payload={"slot": "2026-09-23"})
    await worker.drain()
    brief = next(t for t in c.telegram.texts() if t.startswith("☀️"))
    assert "💊 Сегодня: Омега-3 09:00, 21:00; Магний 22:30\n\n📆 Завтра" in brief  # in time order, with today


@pytest.mark.db
async def test_usage_statistics_button(container: Any) -> None:
    c = container

    async def usage() -> dict[str, Any]:
        return {
            "usage": 0.2221,
            "usage_daily": 0.0727,
            "usage_weekly": 0.2075,
            "usage_monthly": 0.2075,
            "limit": 5,
            "limit_remaining": 4.79,
            "limit_reset": "monthly",
        }

    async def credits() -> dict[str, Any]:
        return {"total_credits": 20, "total_usage": 7.948783504}

    c.llm.usage = usage
    c.llm.credits = credits
    bot = BotService(c)
    await bot.handle_update(msg("напомни позвонить в банк завтра в 10", 50))
    await bot.handle_update(msg("📊 Статистика", 51))
    sent = c.telegram.sent[-1]
    text = sent["text"]
    assert sent["parse_mode"] == "HTML"
    assert text.startswith("📊 <b>Статистика JARVIS</b> · за 30 дней\n\n<pre>")  # no «Минуту, сэр» line above
    table = text.split("<pre>")[1].split("</pre>")[0].split("\n")
    assert table[0].split() == ["30", "дн", "всё", "время"]  # the period next to all time
    assert table[1].split() == ["Сообщения", "2", "2"]
    assert "Задач создано" in table[7] and table[7].split()[-2:] == ["1", "1"]
    assert table[-1].split() == ["Расход", "$0.22", "$0.22"]  # no paid call before the period: exact
    assert len({len(row) for row in table}) == 1  # aligned columns
    assert "💳 Баланс OpenRouter: <b>$12.05</b> (весь аккаунт)" in text
    assert "🔒 Лимит Jarvis на месяц: ▰▰▰▰▰▰▰▰▰▰ <b>$4.79</b> из $5.00" in text
    labels = [[b["text"] for b in row] for row in sent["reply_markup"]["inline_keyboard"]]
    assert labels == [["24 ч", "7 дн", "• 30 дн", "Всё время"]]  # one row (3/6 months still answered)
    await bot.handle_update(tap("us:all", 52))  # switches the same message
    edit = c.telegram.edits[-1]
    assert edit["text"].startswith("📊 <b>Статистика JARVIS</b> · за всё время")
    assert edit["text"].split("<pre>")[1].split("\n")[0].split() == ["всё", "время"]  # one column
    assert len(c.telegram.sent) == 2  # nothing new was sent


@pytest.mark.db
async def test_new_keyboard_layout_is_sent_once(db: Any, clock: Any) -> None:
    from jarvis.bot.menu import KEYBOARD_PREF_KEY
    from jarvis.services.container import build_container
    from tests.conftest import make_settings

    c = build_container(make_settings(menu_keyboard_enabled=True), db=db, clock=clock)
    async with c.db.session() as s:
        s.add(UserPreference(key=KEYBOARD_PREF_KEY, value={"version": "1"}))  # the user has the old layout
    bot = BotService(c)
    await bot.handle_update(msg("статистика", 60))
    keyboard = c.telegram.sent[-1]["reply_markup"]["keyboard"]
    assert [[b["text"] for b in row] for row in keyboard] == [
        ["📅 Сегодня", "📋 Задачи", "📓 Дневник"],
        ["🛒 Покупки", "⋯ Ещё"],
    ]
    count = len(c.telegram.sent)
    await bot.handle_update(msg("статистика", 61))
    assert len(c.telegram.sent) == count + 1


@pytest.mark.db
async def test_a_course_with_an_end_stops_says_so_and_can_be_extended(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("напоминай пить железо в 9:00 курсом две недели", 30))
    assert c.telegram.texts()[-1].splitlines()[:2] == [
        "💊 Буду напоминать: Железо 09:00.",
        "🗓 Курс: 2 недели, до 06.10.2026 — потом напоминания остановлю и скажу.",
    ]
    async with c.db.session() as s:
        [med] = (await s.scalars(select(Medication))).all()
    assert med.ends_on == datetime(2026, 10, 6).date() and med.times == ["09:00"]  # «2 недели» is not 02:00
    await bot.handle_update(msg("мои лекарства", 31))
    assert "курс до 06.10.2026, осталось 14 дн." in c.telegram.texts()[-1]
    assert await tick(c, worker) == ["💊 Время принять: Железо (09:00)."]
    c.clock.advance(timedelta(days=14))  # 07.10 09:00: the course ended yesterday
    assert await tick(c, worker) == [
        "🗓 Курс Железо закончился 06.10.2026 — напоминания остановил.\n"
        "Продлить: «продли железо на 2 недели» (в течение 30 дней)."
    ]
    assert await tick(c, worker) == []  # said once, no intake for 07.10
    await bot.handle_update(msg("продли железо на неделю", 32))
    assert c.telegram.texts()[-1] == "🗓 Продлил курс Железо на 1 неделю: теперь до 13.10.2026."
    async with c.db.session() as s:
        [med] = (await s.scalars(select(Medication))).all()
    assert med.active and med.ends_on == datetime(2026, 10, 13).date()


async def test_a_past_end_is_asked_again(container: Any) -> None:
    c = container
    await BotService(c).handle_update(msg("напоминай пить цинк в 9:00 до 10.09", 40))
    assert c.telegram.texts()[-1].startswith("Срок курса не понял: дата окончания уже прошла")
    async with c.db.session() as s:
        assert (await s.scalars(select(Medication))).all() == []
