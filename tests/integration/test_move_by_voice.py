"""Moving tasks by words, as with ChatGPT (2026-10-01): no date buttons; shifts, follow-up answers, loose names.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from jarvis.bot.service import BotService
from jarvis.db.models import Approval

pytestmark = pytest.mark.db
USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")


def iso(day: int, hour: int, minute: int = 0) -> str:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


async def seeded(c: Any) -> BotService:
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-24", "datetime": iso(24, 15)})
    c.todoist.add(id="FLAT", content="Навести порядок в квартире", due={"date": "2026-09-23"})
    c.todoist.add(id="OLD", content="Позвонить в банк", due={"date": "2026-09-20", "datetime": iso(20, 10)})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    return BotService(c)


def updates(c: Any, task_id: str) -> list[dict[str, Any]]:
    return [call[1][1] for call in c.todoist.calls if call[0] == "update" and call[1][0] == task_id]


@pytest.mark.parametrize(
    ("said", "task_id", "expected", "reply"),
    [
        # a timed task ahead keeps moving from its own time
        ("перенеси стрижку на 2 часа позже", "HAIR", {"due_datetime": iso(24, 17)}, "завтра, чт 24.09 17:00"),
        ("стрижку сдвинь на полчаса раньше", "HAIR", {"due_datetime": iso(24, 14, 30)}, "завтра, чт 24.09 14:30"),
        ("Джарвис, стрижку отложим на пару дней", "HAIR", {"due_datetime": iso(26, 15)}, "сб 26.09 15:00"),
        ("перенеси стрижку на выходные", "HAIR", {"due_datetime": iso(26, 15)}, "сб 26.09 15:00"),
        ("перенеси стрижку на пятницу в шесть вечера", "HAIR", {"due_datetime": iso(25, 18)}, "пт 25.09 18:00"),
        # an overdue task moves from now: «через 2 часа» / «на 2 часа» = in two hours (rounded)
        ("перенеси звонок в банк на 2 часа", "OLD", {"due_datetime": iso(23, 11)}, "сегодня, ср 23.09 11:00"),
        ("перенеси порядок в квартире на неделю", "FLAT", {"due_date": "2026-09-30"}, "ср 30.09"),
    ],
)
async def test_move_by_shift_and_phrase(container: Any, said: str, task_id: str, expected: Any, reply: str) -> None:
    c = container
    bot = await seeded(c)
    await bot.handle_update(msg(said, 1))
    assert updates(c, task_id) and {k: v for k, v in updates(c, task_id)[-1].items() if k in expected} == expected
    assert c.telegram.texts()[-1].endswith(reply)
    assert not c.telegram.sent[-1].get("reply_markup")  # no buttons at all


async def test_date_question_is_answered_by_the_next_message(container: Any) -> None:
    c = container
    bot = await seeded(c)
    await bot.handle_update(msg("постричься перенесём", 10))
    question = c.telegram.sent[-1]
    assert c.telegram.texts()[-1].startswith("🗓 На когда перенести «Постричься»?")
    assert not question.get("reply_markup")
    assert updates(c, "HAIR") == []  # no date said → nothing guessed
    await bot.handle_update(msg("давай в субботу утром", 11))
    assert updates(c, "HAIR")[-1]["due_datetime"] == iso(26, 9)
    await bot.handle_update(msg("в пятницу", 12))  # the question was answered: a bare date is not a move any more
    assert len(updates(c, "HAIR")) == 1


async def test_another_request_instead_of_an_answer_and_cancel(container: Any) -> None:
    c = container
    bot = await seeded(c)
    await bot.handle_update(msg("перенеси стрижку", 20))
    await bot.handle_update(msg("добавь задачу позвонить маме", 21))  # not a date: handled as a new request
    assert ("create", ("Позвонить маме", None)) in c.todoist.calls
    await bot.handle_update(msg("на завтра", 22))  # the question no longer waits
    assert updates(c, "HAIR") == []
    await bot.handle_update(msg("перенеси порядок в квартире", 23))
    await bot.handle_update(msg("не надо", 24))
    assert c.telegram.texts()[-1] == "Ок, оставляю как есть."
    assert updates(c, "FLAT") == []


async def test_loose_name_is_resolved_by_meaning(container: Any) -> None:
    """«поход к парикмахеру» shares no word with «Постричься»: the LLM picks a title from the list, SQL resolves it."""
    c = container
    bot = await seeded(c)
    seen: list[str] = []

    def pick(user: str, _messages: Any) -> dict[str, Any]:
        seen.append(user)
        return {"titles": ["Постричься"]}

    c.llm.responders["task_reference"] = pick
    await bot.handle_update(msg("перенеси поход к парикмахеру на завтра в 18", 30))
    assert updates(c, "HAIR")[-1]["due_datetime"] == iso(24, 18)
    assert "<phrase>поход к парикмахеру</phrase>" in seen[0] and "- Постричься" in seen[0]


async def test_invented_title_is_not_trusted(container: Any) -> None:
    c = container
    bot = await seeded(c)
    c.llm.responders["task_reference"] = lambda _u, _m: {"titles": ["Сходить в барбершоп"]}  # not a real task
    await bot.handle_update(msg("перенеси барбершоп на завтра", 40))
    assert c.telegram.texts()[-1] == "Не нашёл задачу «барбершоп». Уточни название."
    assert not [call for call in c.todoist.calls if call[0] == "update"]


async def test_several_tasks_by_meaning_need_confirmation(container: Any) -> None:
    c = container
    bot = await seeded(c)
    c.llm.responders["task_reference"] = lambda _u, _m: {"titles": ["Постричься", "Навести порядок в квартире"]}
    await bot.handle_update(msg("перенеси салон и генеральную уборку на субботу", 50))
    text = c.telegram.texts()[-1]
    assert text.startswith("➡️ Перенести (2)?")
    assert "• Постричься → сб 26.09 15:00" in text and "• Навести порядок в квартире → сб 26.09" in text
    assert not [call for call in c.todoist.calls if call[0] == "update"]  # nothing before the button
    async with c.db.session() as s:
        assert (await s.scalar(select(Approval))).status == "pending"  # type: ignore[union-attr]
