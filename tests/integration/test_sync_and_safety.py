"""Sync correctness (no loops, out-of-order safety, push channels) and cross-cutting safety invariants."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from jarvis.core.clock import FrozenClock, NaiveDatetimeError
from jarvis.core.config import CalendarStrategy
from jarvis.core.logging import REDACTOR, RedactingFilter
from jarvis.db.models import CalendarChannel, CalendarEvent, ScheduledJob, TodoistTask
from jarvis.db.session import Database
from jarvis.integrations.todoist.client import parse_task
from jarvis.scheduler.queue import JobQueue, enqueue
from jarvis.scheduler.runner import HandlerRegistry, JobContext, Worker
from jarvis.sync.calendar_sync import mirror_tasks
from jarvis.sync.todoist_sync import upsert_task

pytestmark = pytest.mark.db
TZ = ZoneInfo("Asia/Tbilisi")


def iso(h: int, m: int = 0) -> str:
    return datetime(2026, 9, 23, h, m, tzinfo=TZ).astimezone(UTC).isoformat().replace("+00:00", "Z")


async def test_out_of_order_snapshot_does_not_regress(container: Any) -> None:
    c = container
    newer = parse_task({"id": "T", "content": "new title", "updated_at": "2026-09-23T10:00:00Z"}, TZ)
    older = parse_task({"id": "T", "content": "old title", "updated_at": "2026-09-23T09:00:00Z"}, TZ)
    async with c.db.session() as s:
        assert await upsert_task(s, newer, c.clock.now())
        assert not await upsert_task(s, older, c.clock.now())
        task = await s.get(TodoistTask, "T")
        assert task is not None
        assert task.content == "new title"


async def test_reconcile_detects_completed_and_deleted(container: Any, worker: Any) -> None:
    c = container
    c.todoist.add(id="A", content="stays")
    c.todoist.add(id="B", content="will complete")
    c.todoist.add(id="C", content="will be deleted")
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
    c.todoist.tasks["B"]["checked"] = True
    del c.todoist.tasks["C"]
    async with c.db.session() as s:
        stats = await c.todoist_sync.reconcile(s, c.clock.now())
        rows = {t.id: (t.checked, t.is_deleted) for t in (await s.scalars(select(TodoistTask))).all()}
    assert stats == {"active": 1, "changed": 0, "vanished": 2}
    assert rows == {"A": (False, False), "B": (True, False), "C": (False, True)}


async def test_reconcile_writes_only_what_changed(container: Any) -> None:
    # L11 (review 17.10): every two minutes every task was rewritten.
    c = container
    c.todoist.add(id="A", content="stays")
    c.todoist.add(id="B", content="renamed soon")
    async with c.db.session() as s:
        assert (await c.todoist_sync.reconcile(s, c.clock.now()))["changed"] == 2
    first = c.clock.now()
    c.clock.advance(timedelta(minutes=2))
    c.todoist.tasks["B"]["content"] = "renamed"
    async with c.db.session() as s:
        assert (await c.todoist_sync.reconcile(s, c.clock.now()))["changed"] == 1
        rows = {t.id: (t.content, t.synced_at) for t in (await s.scalars(select(TodoistTask))).all()}
    assert rows == {"A": ("stays", first), "B": ("renamed", c.clock.now())}


async def test_calendar_cancellation_and_exclusions(container: Any) -> None:
    c = container
    c.calendar.calendars.append({"id": "todoist-cal", "summary": "Todoist"})
    c.settings.google_calendar_ids = ["primary", "todoist-cal"]
    c.calendar.add_event("primary", summary="A", start={"dateTime": iso(12)}, end={"dateTime": iso(13)})
    c.calendar.add_event("todoist-cal", summary="task", start={"dateTime": iso(14)}, end={"dateTime": iso(15)})
    async with c.db.session() as s:
        stats = await c.calendar_sync.reconcile(s, c.clock.now())
    assert stats["calendars"] == 1
    assert stats["changed"] == 1
    async with c.db.session() as s:
        stats = await c.calendar_sync.reconcile(s, c.clock.now())
    assert stats == {"changed": 0, "unchanged": 1, "removed": 0, "calendars": 1}  # etag unchanged → no writes
    c.calendar.events["primary"].clear()  # deleted upstream (missed notification)
    async with c.db.session() as s:
        stats = await c.calendar_sync.reconcile(s, c.clock.now())
        status = await s.scalar(select(CalendarEvent.status))
    assert stats["removed"] == 1
    assert status == "cancelled"


async def test_push_channels_created_renewed_and_verified(container: Any) -> None:
    c = container
    c.settings.jarvis_public_base_url = "https://jarvis.example.com"
    async with c.db.session() as s:
        assert await c.calendar_sync.ensure_channels(s, c.clock.now()) == {"created": 1, "renewed": 0}
    async with c.db.session() as s:
        assert await c.calendar_sync.ensure_channels(s, c.clock.now()) == {"created": 0, "renewed": 0}
        channel = await s.scalar(select(CalendarChannel))
        assert channel is not None
        channel.expiration = c.clock.now() + timedelta(hours=2)  # about to expire
    async with c.db.session() as s:
        assert await c.calendar_sync.ensure_channels(s, c.clock.now()) == {"created": 1, "renewed": 1}
        active = (await s.scalars(select(CalendarChannel).where(CalendarChannel.status == "active"))).all()
    assert len(active) == 1
    headers = {
        "x-goog-channel-token": "gcal-channel-token",
        "x-goog-channel-id": active[0].channel_id,
        "x-goog-message-number": "7",
        "x-goog-resource-state": "exists",
    }
    async with c.db.session() as s:
        assert await c.calendar_sync.accept_notification(s, headers, c.clock.now()) == {"status": "queued"}
        assert await c.calendar_sync.accept_notification(s, headers, c.clock.now()) == {"status": "duplicate"}
    with pytest.raises(PermissionError):
        async with c.db.session() as s:
            await c.calendar_sync.accept_notification(s, {**headers, "x-goog-channel-token": "x"}, c.clock.now())


async def test_mirror_strategy_is_one_way_and_idempotent(container: Any) -> None:
    c = container
    c.settings.calendar_strategy = CalendarStrategy.MIRROR
    c.settings.google_mirror_calendar_id = "mirror"
    c.todoist.add(
        id="T1",
        content="Deep work",
        due={"date": "2026-09-23", "datetime": iso(16)},
        duration={"amount": 90, "unit": "minute"},
    )
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
        assert await mirror_tasks(s, c.calendar, c.settings, c.clock.now()) == {"written": 1, "unchanged": 0}
    async with c.db.session() as s:
        assert await mirror_tasks(s, c.calendar, c.settings, c.clock.now()) == {"written": 0, "unchanged": 1}
    body = c.calendar.mirror_writes[0]["body"]
    assert body["extendedProperties"]["private"] == {"jarvis_managed": "true", "todoist_task_id": "T1"}
    # The mirror calendar is excluded from reconciliation → nothing flows back into Todoist.
    c.settings.google_calendar_ids = ["primary", "mirror"]
    async with c.db.session() as s:
        stats = await c.calendar_sync.reconcile(s, c.clock.now())
    assert stats["calendars"] == 1
    assert not [call for call in c.todoist.calls if call[0] in ("schedule", "due_string")]


async def test_naive_datetime_rejected_by_database_layer(container: Any) -> None:
    with pytest.raises(Exception) as info:  # noqa: PT011 - wrapped by SQLAlchemy StatementError
        async with container.db.session() as s:
            await enqueue(s, job_type="x", run_at=datetime(2026, 1, 1))
    assert isinstance(info.value, NaiveDatetimeError) or "naive" in str(info.value)


def test_log_records_are_redacted() -> None:
    REDACTOR.add(["very-secret-token-value"])
    logger = logging.getLogger("jarvis.test")
    handler = logging.Handler()
    handler.addFilter(RedactingFilter())
    records: list[str] = []
    handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        logger.warning(
            "calling https://api.telegram.org/bot123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/getMe with %s",
            "very-secret-token-value",
        )
    finally:
        logger.removeHandler(handler)
    assert "AAAAAAAAAAAAAAAA" not in records[0]
    assert "very-secret-token-value" not in records[0]


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(keys=st.lists(st.sampled_from([f"k{i}" for i in range(6)]), min_size=1, max_size=15))
async def test_property_duplicate_keys_execute_once(db: Database, keys: list[str]) -> None:
    clock = FrozenClock(datetime(2026, 9, 23, 5, tzinfo=UTC))
    marker = f"prop-{uuid.uuid4().hex}-"  # not id(keys): ids are reused across examples, dedupe keys are forever
    executed: list[str] = []

    async def handler(ctx: JobContext) -> dict[str, Any]:
        executed.append(ctx.payload["key"])
        return {}

    registry = HandlerRegistry()
    registry.register("prop", handler)
    async with db.session() as s:
        for key in keys:
            await enqueue(s, job_type="prop", run_at=clock.now(), payload={"key": key}, dedupe_key=marker + key)
    workers = [Worker(JobQueue(db, clock, worker_id=f"w{i}"), registry, object()) for i in range(3)]  # type: ignore[arg-type]
    for _ in range(3):
        await __import__("asyncio").gather(*(w.run_once(limit=5) for w in workers))
    assert sorted(executed) == sorted(set(keys))
    async with db.session() as s:
        remaining = await s.scalar(
            select(func.count())
            .select_from(ScheduledJob)
            .where(ScheduledJob.dedupe_key.like(marker + "%"), ScheduledJob.status == "pending")
        )
    assert remaining == 0
