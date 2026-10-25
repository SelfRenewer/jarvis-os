"""Persistent reminders — the free answer to «позвони мне, чтобы напомнить» (the Bot API cannot place calls).

A reminder about an event or a timed task carries «👌 Понял · ⏰ 10 мин». Until «Понял» is pressed (or the task is
done, the event cancelled, or its start long past) it is repeated with a sound at `REMINDER_REPEAT_MINUTES` after the
first message; the first repeat also comes as a voice note, and every repeat pops up on an online Mac. «10 мин»
snoozes: the chain stops and one more reminder comes in 10 minutes. The acknowledgement lives in the reminder's
payload (no extra table).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.config import Settings
from jarvis.db.models import CalendarEvent, Reminder, TodoistTask
from jarvis.integrations.telegram.client import InlineButton, inline_keyboard
from jarvis.scheduler.queue import enqueue

SNOOZE = timedelta(minutes=10)
GRACE_AFTER_START = timedelta(minutes=15)  # repeating after the start makes no sense


def buttons(reminder_id: uuid.UUID) -> dict[str, Any]:
    return inline_keyboard(
        [[InlineButton("👌 Понял", f"rk:{reminder_id.hex}"), InlineButton("⏰ 10 мин", f"rz:{reminder_id.hex}")]]
    )


def repeat_offsets(settings: Settings) -> list[int]:
    return sorted({m for m in settings.reminder_repeat_minutes if 0 < m <= 180})


async def schedule_repeats(session: AsyncSession, reminder: Reminder, sent_at: datetime, settings: Settings) -> int:
    """After the first message: the whole repeat chain as jobs (each re-checks before sending). Idempotent."""
    if not settings.reminder_escalation_enabled:
        return 0
    offsets = repeat_offsets(settings)
    for step, minutes in enumerate(offsets, start=1):
        await enqueue(
            session,
            job_type="reminder.repeat",
            run_at=sent_at + timedelta(minutes=minutes),
            payload={"reminder_id": str(reminder.id), "step": step, "of": len(offsets)},
            dedupe_key=f"reminder.repeat:{reminder.id}:{step}",
        )
    return len(offsets)


async def still_relevant(session: AsyncSession, reminder: Reminder, now: datetime, tz: Any) -> str | None:
    """None when a repeat should go out; otherwise why not."""
    payload = reminder.payload or {}
    if payload.get("ack_at"):
        return "acknowledged"
    if reminder.status != "sent":
        return f"status {reminder.status}"
    if now > reminder.occurrence_start + GRACE_AFTER_START:
        return "started long ago"
    if reminder.source_type == "todoist_task":
        task = await session.get(TodoistTask, reminder.source_id)
        if task is None or task.checked or task.is_deleted:
            return "task done"
        return None
    event_uuid = payload.get("event_uuid")
    event = await session.get(CalendarEvent, uuid.UUID(event_uuid)) if event_uuid else None
    if event is None or event.status == "cancelled":
        return "event cancelled"
    from jarvis.sync.links import linked_tasks

    links = await linked_tasks(session, event, tz)
    if links and all(t.checked for t in links):
        return "linked task done"
    return None


async def acknowledge(session: AsyncSession, reminder_id: uuid.UUID, now: datetime, *, snooze: bool) -> Reminder | None:
    """«👌 Понял» / «⏰ 10 мин»: stop the chain (exactly once); a snooze schedules one more reminder."""
    reminder = await session.get(Reminder, reminder_id, with_for_update=True)
    if reminder is None:
        return None
    payload = dict(reminder.payload or {})
    if snooze:
        count = int(payload.get("snoozes") or 0) + 1
        payload["snoozes"] = count
        payload.pop("ack_at", None)
        payload["chain_stopped_at"] = now.isoformat()
        await enqueue(
            session,
            job_type="reminder.repeat",
            run_at=now + SNOOZE,
            payload={"reminder_id": str(reminder.id), "step": "snooze", "of": 0},
            dedupe_key=f"reminder.repeat:{reminder.id}:snooze:{count}",
        )
    else:
        payload["ack_at"] = now.isoformat()
    reminder.payload = payload
    return reminder
