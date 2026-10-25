"""Persistent reminders (the user's choice «а», 2026-10-14 — instead of calls, which the Bot API cannot place):
a reminder repeats with a sound until «👌 Понял»; the first repeat also comes as a voice note; «⏰ 10 мин»
snoozes; a task done in Todoist, a cancelled event or a start long past ends the chain.

Clock: Wed 2026-09-23 09:00 Asia/Tbilisi."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from jarvis.bot.service import BotService
from jarvis.db.models import ScheduledJob
from jarvis.scheduler.queue import enqueue

pytestmark = pytest.mark.db
USER = 1001
TZ = ZoneInfo("Asia/Tbilisi")


def local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 23, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


async def drain_until(c: Any, worker: Any, until: datetime) -> None:
    while True:
        async with c.db.session() as s:
            next_run = await s.scalar(select(func.min(ScheduledJob.run_at)).where(ScheduledJob.status == "pending"))
        if next_run is None or next_run > until:
            break
        c.clock.set(max(next_run, c.clock.now()))
        await worker.drain()
    c.clock.set(until)


async def meeting(c: Any, worker: Any, start: datetime, summary: str = "Встреча с Ниной") -> None:
    c.settings.voice_calls_enabled = False
    c.settings.reminder_offsets_minutes = [30]
    c.calendar.add_event(
        "primary", summary=summary, start={"dateTime": iso(start)}, end={"dateTime": iso(start + timedelta(hours=1))}
    )
    async with c.db.session() as s:
        await enqueue(s, job_type="calendar.reconcile", run_at=c.clock.now())
    await worker.drain()


def reminders_sent(c: Any) -> list[dict[str, Any]]:
    """First reminders only (repeats start with «🔔 Напоминаю ещё раз» / «⏰ Как просили»)."""
    return [m for m in c.telegram.sent if (m.get("text") or "").startswith("⏰ Через")]


def buttons(message: dict[str, Any]) -> dict[str, str]:
    markup = message.get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


async def test_reminder_repeats_until_understood(container: Any, worker: Any) -> None:
    c = container
    await meeting(c, worker, local(11, 0))
    await drain_until(c, worker, local(10, 45))  # the reminder 15 min before
    [first] = reminders_sent(c)
    assert set(buttons(first)) == {"👌 Понял", "⏰ 10 мин"}
    await drain_until(c, worker, local(10, 48))  # +3 min: first repeat, with a voice note
    texts = [m.get("text") or "" for m in c.telegram.sent]
    assert any(t.startswith("<b>🔔 Напоминаю ещё раз (1/3)</b>") and "Встреча с Ниной" in t for t in texts)
    assert any(m.get("voice") for m in c.telegram.sent)  # a voice note rings differently
    assert c.tts.calls and "Сэр, напоминаю" in c.tts.calls[-1][0]
    await drain_until(c, worker, local(10, 53))  # +8 min: second repeat
    assert sum(1 for t in [m.get("text") or "" for m in c.telegram.sent] if "Напоминаю ещё раз" in t) == 2
    await BotService(c).handle_update(tap(buttons(first)["👌 Понял"], 1))
    assert c.telegram.answered[-1][1] == "👌 Принято, больше не напоминаю"
    sent = len(c.telegram.sent)
    await drain_until(c, worker, local(11, 10))
    assert len(c.telegram.sent) == sent  # the 15-min repeat is cancelled by «Понял»


async def test_snooze_replaces_the_chain_with_one_reminder_in_ten_minutes(container: Any, worker: Any) -> None:
    c = container
    await meeting(c, worker, local(11, 0))
    await drain_until(c, worker, local(10, 45))
    [first] = reminders_sent(c)
    await BotService(c).handle_update(tap(buttons(first)["⏰ 10 мин"], 2))
    assert c.telegram.answered[-1][1] == "⏰ Напомню через 10 минут"
    await drain_until(c, worker, local(10, 54))
    assert not any("Напоминаю ещё раз" in (m.get("text") or "") for m in c.telegram.sent)  # the chain is stopped
    await drain_until(c, worker, local(10, 56))
    assert "Как просили — напоминаю" in (c.telegram.sent[-1].get("text") or "")
    await drain_until(c, worker, local(11, 10))
    assert sum(1 for m in c.telegram.sent if "Как просили" in (m.get("text") or "")) == 1


async def test_done_task_and_past_start_end_the_chain(container: Any, worker: Any) -> None:
    c = container
    c.settings.voice_calls_enabled = False
    c.settings.reminder_task_offsets_minutes = [0]
    c.todoist.add(id="CALL", content="Позвонить в банк", due={"date": "2026-09-23", "datetime": iso(local(10, 0))})
    async with c.db.session() as s:
        await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now())
    await worker.drain()
    async with c.db.session() as s:
        await enqueue(s, job_type="reminders.plan", run_at=c.clock.now())
    await worker.drain()
    await drain_until(c, worker, local(10, 0))
    assert any("Позвонить в банк" in (m.get("text") or "") for m in c.telegram.sent)
    sent = len(c.telegram.sent)
    await c.todoist.close_task("CALL", request_id="app")  # done in the Todoist app, no poll in between
    await drain_until(c, worker, local(10, 20))
    assert len(c.telegram.sent) == sent


async def test_escalation_can_be_switched_off(container: Any, worker: Any) -> None:
    c = container
    c.settings.reminder_escalation_enabled = False
    await meeting(c, worker, local(11, 0))
    await drain_until(c, worker, local(10, 59))
    [first] = reminders_sent(c)
    assert buttons(first) == {}
    assert not any("Напоминаю ещё раз" in (m.get("text") or "") for m in c.telegram.sent)
