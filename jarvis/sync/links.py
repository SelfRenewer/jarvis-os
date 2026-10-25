"""A Todoist task and a hand-made calendar event that are the same item (the user keeps many things in both).

Linked when (R11/R15 rules):
* a timed task is within ±30 min of the event start and the titles are the same item (`same_item_titles`), or
* the task is due on the event's local day and the titles are the same item, or the event title contains every
  word of the task title (`covered_by`: one event «Купить батарейки и пластыри» for two tasks).

An event is **done** when it has linked tasks and all of them are completed in Todoist: completing the task in the
Todoist app / ChatGPT / the bot then also silences the event (reminders, agenda). Deleted tasks do not count.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.clock import to_local
from jarvis.core.text import covered_by, same_item_titles
from jarvis.db.models import CalendarEvent, TodoistTask

LINK_TOLERANCE = timedelta(minutes=30)


def same_item(task: TodoistTask, summary: str | None, start: datetime | None, tz: ZoneInfo) -> bool:
    if start is None or not summary or task.is_deleted:
        return False
    near = task.due_datetime is not None and abs(task.due_datetime - start) <= LINK_TOLERANCE
    if near and same_item_titles(summary, task.content):
        return True
    if task.due_date is not None and task.due_date == to_local(start, tz).date():
        return same_item_titles(summary, task.content) or covered_by(task.content, summary)
    return False


def linked(event: CalendarEvent, tasks: Iterable[TodoistTask], tz: ZoneInfo) -> list[TodoistTask]:
    return [t for t in tasks if same_item(t, event.summary, event.start_at, tz)]


def done_by_tasks(event: CalendarEvent, tasks: Iterable[TodoistTask], tz: ZoneInfo) -> bool:
    links = linked(event, tasks, tz)
    return bool(links) and all(t.checked for t in links)


async def tasks_for_linking(
    session: AsyncSession, first: datetime, last: datetime, tz: ZoneInfo
) -> Sequence[TodoistTask]:
    """Open and completed (not deleted) tasks due around [first, last] — candidates for `linked`."""
    start_day = to_local(first, tz).date() - timedelta(days=1)
    end_day = to_local(last, tz).date() + timedelta(days=1)
    return (
        await session.scalars(
            select(TodoistTask).where(
                TodoistTask.is_deleted.is_(False),
                TodoistTask.due_date >= start_day,
                TodoistTask.due_date <= end_day,
            )
        )
    ).all()


async def linked_tasks(session: AsyncSession, event: CalendarEvent, tz: ZoneInfo) -> list[TodoistTask]:
    if event.start_at is None:
        return []
    return linked(event, await tasks_for_linking(session, event.start_at, event.start_at, tz), tz)
