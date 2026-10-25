"""Regression tests for defects found in the post-implementation review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from jarvis.approvals.service import APPROVE, decide
from jarvis.bot.service import BotService
from jarvis.core.clock import FrozenClock
from jarvis.core.errors import AuthError
from jarvis.db.models import PlanningProposalItem, Reminder, ScheduledJob
from jarvis.integrations.maps.routes import RouteStatus
from jarvis.scheduler.queue import enqueue

pytestmark = pytest.mark.db
TZ = ZoneInfo("Asia/Tbilisi")


def local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 23, h, m, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _event(c: Any, worker: Any, start: datetime) -> None:
    c.routes.durations[(c.settings.default_origin, "Vake Park, Tbilisi")] = 22 * 60
    c.calendar.add_event(
        "primary",
        summary="Встреча",
        location="Vake Park, Tbilisi",
        start={"dateTime": iso(start)},
        end={"dateTime": iso(start + timedelta(hours=1))},
    )
    async with c.db.session() as s:
        await enqueue(s, job_type="calendar.reconcile", run_at=c.clock.now())
    await worker.drain()


async def _plan(c: Any, worker: Any) -> None:
    async with c.db.session() as s:
        await enqueue(s, job_type="reminders.plan", run_at=c.clock.now())
    await worker.drain()


async def test_periodic_replanning_does_not_requery_maps(container: Any, worker: Any, clock: FrozenClock) -> None:
    """R1: the cache key used to depend on `now` → a billable Maps request on almost every 5-min re-plan."""
    c = container
    await _event(c, worker, local(15, 0))
    for _ in range(24):  # two hours of periodic planning
        clock.advance(timedelta(minutes=5))
        await _plan(c, worker)
    assert len(c.routes.calls) == 1


async def test_single_traffic_refresh_before_departure(container: Any, worker: Any, clock: FrozenClock) -> None:
    c = container
    await _event(c, worker, local(15, 0))
    clock.set(local(13, 48))  # leave_at 14:28 − 40 min
    await worker.drain()
    assert len(c.routes.calls) == 2  # planning estimate + exactly one forced traffic refresh


async def test_maps_outage_keeps_leave_reminders(container: Any, worker: Any, clock: FrozenClock) -> None:
    """R2: a quota/outage used to flip scheduled 'leave' reminders to 'start' ones (churn + worse timing)."""
    c = container
    await _event(c, worker, local(15, 0))
    c.routes.durations.clear()
    c.routes.default_status = RouteStatus.QUOTA
    clock.set(local(13, 48))  # forced refresh fails with quota
    await worker.drain()
    async with c.db.session() as s:
        rows = (await s.scalars(select(Reminder).where(Reminder.status == "scheduled"))).all()
        cancelled = await s.scalar(select(func.count()).select_from(Reminder).where(Reminder.status == "cancelled"))
    assert {r.kind for r in rows} == {"leave"}
    assert sorted(r.fire_at for r in rows)[-1] == local(14, 28).astimezone(UTC)
    assert cancelled == 0


async def test_stale_plan_never_schedules_into_the_past(container: Any, worker: Any, clock: FrozenClock) -> None:
    """R3: approving an old proposal must not write slots that already passed."""
    c = container
    c.todoist.add(id="T1", content="Задача", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now())
    await worker.drain()
    async with c.db.session() as s:
        outcome = await c.planner.propose(s, c.clock.now(), requested_by="test")
    clock.advance(timedelta(hours=11))  # approval still valid (12h TTL) but the slot is gone
    async with c.db.session() as s:
        decision = await decide(s, nonce=outcome.approval_nonce or "", decision=APPROVE, actor="t", now=c.clock.now())
        assert decision is not None
        await c.planner.approve(s, outcome.proposal_id, c.clock.now())  # type: ignore[arg-type]
    await worker.drain()
    assert not [call for call in c.todoist.calls if call[0] == "schedule"]
    async with c.db.session() as s:
        item = await s.scalar(select(PlanningProposalItem).where(PlanningProposalItem.action == "schedule"))
    assert item is not None
    assert item.apply_status == "skipped"


async def test_revoked_credentials_alert_once_and_do_not_dead_letter(
    container: Any, worker: Any, clock: FrozenClock
) -> None:
    """R4: revoked Google/Todoist auth used to dead-letter a job every period with no user-visible alert."""
    c = container

    async def denied(*args: Any, **kwargs: Any) -> Any:
        raise AuthError("todoist: HTTP 401 (authentication/authorization failed)")

    c.todoist.list_active_tasks = denied
    for _ in range(3):
        async with c.db.session() as s:
            await enqueue(s, job_type="todoist.reconcile", run_at=c.clock.now())
        await worker.drain()
        clock.advance(timedelta(minutes=10))
    alerts = [t for t in c.telegram.texts() if "авторизация недействительна" in t]
    assert len(alerts) == 1
    async with c.db.session() as s:
        dead = await s.scalar(select(func.count()).select_from(ScheduledJob).where(ScheduledJob.status == "dead"))
    assert dead == 0


async def test_unexpected_bot_error_gets_generic_reply(container: Any) -> None:
    """R5: non-Jarvis exceptions used to escape silently (message lost without feedback)."""
    bot = BotService(container)

    async def boom(chat_id: int) -> None:
        raise RuntimeError("bug with secret-ish detail")

    bot.cmd_today = boom  # type: ignore[method-assign]
    update = {
        "update_id": 900,
        "message": {"message_id": 1, "text": "/today", "from": {"id": 1001}, "chat": {"id": 1001, "type": "private"}},
    }
    assert await bot.handle_update(update) == "error"
    assert container.telegram.texts() == ["⚠️ Внутренняя ошибка. Подробности в логах сервера."]


async def test_planner_blocks_travel_time_before_located_events(
    container: Any, worker: Any, clock: FrozenClock
) -> None:
    """R6 (found by scripts/demo.py): a task was proposed for 14:15–14:45 although the user must leave at 14:28."""
    c = container
    await _event(c, worker, local(15, 0))  # leave at 14:28 (22 min travel + 10 min buffer)
    c.calendar.add_event(
        "primary", summary="Работа", start={"dateTime": iso(local(9))}, end={"dateTime": iso(local(14, 0))}
    )
    c.calendar.add_event(
        "primary",
        summary="Без адреса",
        start={"dateTime": iso(local(17, 0))},
        end={"dateTime": iso(local(18, 0))},
        location="Somewhere unknown",
    )
    c.todoist.add(
        id="T1", content="Оплатить интернет", due={"date": "2026-09-23"}, duration={"amount": 30, "unit": "minute"}
    )
    for job in ("todoist.reconcile", "calendar.reconcile"):
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    async with c.db.session() as s:
        outcome = await c.planner.propose(s, c.clock.now(), requested_by="test")
        item = await s.scalar(select(PlanningProposalItem).where(PlanningProposalItem.action == "schedule"))
    assert outcome.scheduled == 1
    assert item is not None
    assert item.start_at is not None
    assert item.end_at is not None
    # Not inside the drive (14:28–15:00) + meeting, nor inside the 30-min unknown-travel block before 17:00.
    assert item.end_at <= local(14, 18).astimezone(UTC) or item.start_at >= local(16, 10).astimezone(UTC)
    assert not (local(16, 20).astimezone(UTC) < item.end_at and item.start_at < local(18, 10).astimezone(UTC))


async def test_task_mirrored_as_calendar_event_gets_single_reminder(container: Any, worker: Any) -> None:
    """R11 (found on the live account): the user keeps the same item in Todoist and Google Calendar →
    two reminders per item. The calendar event's reminder wins; the task's is suppressed."""
    c = container
    c.calendar.add_event(
        "primary",
        summary="Уточнить у владельца вызов мастера",
        start={"dateTime": iso(local(10, 0))},
        end={"dateTime": iso(local(10, 15))},
    )
    c.todoist.add(
        id="T1",
        content="уточнить у владельца  вызов мастера.",  # same item, sloppy spelling
        due={"date": "2026-09-23", "datetime": iso(local(10, 0))},
    )
    c.todoist.add(id="T2", content="Отдельная задача", due={"date": "2026-09-23", "datetime": iso(local(11, 0))})
    for job in ("todoist.reconcile", "calendar.reconcile", "reminders.plan"):
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    async with c.db.session() as s:
        rows = (await s.scalars(select(Reminder).where(Reminder.status == "scheduled"))).all()
    assert sorted((r.source_type, r.fire_at) for r in rows) == [
        ("calendar_event", local(9, 45).astimezone(UTC)),
        ("todoist_task", local(11, 0).astimezone(UTC)),
    ]


async def test_planner_skips_tasks_already_in_calendar(container: Any, worker: Any) -> None:
    c = container
    c.calendar.add_event(
        "primary", summary="Постричься", start={"dateTime": iso(local(18, 0))}, end={"dateTime": iso(local(19, 0))}
    )
    c.todoist.add(id="T1", content="Постричься", due={"date": "2026-09-23"})
    c.todoist.add(id="T2", content="Оплатить интернет", due={"date": "2026-09-23"})
    for job in ("todoist.reconcile", "calendar.reconcile"):
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    async with c.db.session() as s:
        outcome = await c.planner.propose(s, c.clock.now(), requested_by="test")
        items = {i.todoist_task_id: (i.action, i.reason) for i in (await s.scalars(select(PlanningProposalItem))).all()}
    assert outcome.scheduled == 1
    assert items["T1"] == ("skip", "already_in_calendar")
    assert items["T2"][0] == "schedule"
    assert "Уже стоят в календаре" in outcome.text


def test_normalize_title() -> None:
    from jarvis.core.text import normalize_title

    assert normalize_title("  Уточнить у владельца  вызов мастера. ") == normalize_title(
        "уточнить у владельца вызов мастера"
    )
    assert normalize_title("Ёлка!") == "елка"
    assert normalize_title(None) == ""


def test_same_item_titles_rules() -> None:
    from jarvis.core.text import same_item_titles

    assert same_item_titles("Уточнить у владельца вызов мастера", "Уточнить у владельца квартиры вызов мастера")
    assert same_item_titles("Постричься", "постричься!")
    assert not same_item_titles("Позвонить маме", "Позвонить маме и папе насчёт поездки")  # < 3 words in the smaller
    assert not same_item_titles("Купить молоко и хлеб", "Купить молоко и сыр")


async def test_near_duplicate_title_at_same_time_is_linked(container: Any, worker: Any) -> None:
    c = container
    c.calendar.add_event(
        "primary",
        summary="Уточнить у владельца вызов мастера",
        start={"dateTime": iso(local(10, 0))},
        end={"dateTime": iso(local(10, 15))},
    )
    c.todoist.add(
        id="T1",
        content="Уточнить у владельца квартиры вызов мастера",
        due={"date": "2026-09-23", "datetime": iso(local(10, 0))},
    )
    for job in ("todoist.reconcile", "calendar.reconcile", "reminders.plan"):
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    async with c.db.session() as s:
        sources = sorted((await s.scalars(select(Reminder.source_type).where(Reminder.status == "scheduled"))).all())
    assert sources == ["calendar_event"]


async def test_planner_skips_items_placed_beyond_horizon_or_merged(container: Any, worker: Any) -> None:
    """R15 (found live while verifying the morning briefing): the plan re-planned tasks the user had already put
    in the calendar — an event 3 days ahead (outside the 3-day horizon, near-duplicate title) and one event that
    merges two tasks («Купить батарейки и пластыри»)."""
    c = container
    day26 = datetime(2026, 9, 26, 13, 0, tzinfo=TZ)
    c.calendar.add_event(
        "primary",
        summary="Изучить ВНЖ Грузии через ИП или бизнес",
        start={"dateTime": iso(day26)},
        end={"dateTime": iso(day26 + timedelta(minutes=45))},
    )
    c.calendar.add_event(
        "primary",
        summary="Купить батарейки и пластыри",
        start={"dateTime": iso(local(14, 15))},
        end={"dateTime": iso(local(14, 45))},
    )
    # A past event with the same title does not count: the task was moved on purpose.
    c.calendar.add_event(
        "primary", summary="Постричься", start={"dateTime": iso(local(6, 0))}, end={"dateTime": iso(local(6, 30))}
    )
    c.todoist.add(id="VNJ", content="Изучить получение ВНЖ Грузии через ИП или бизнес", due={"date": "2026-09-23"})
    c.todoist.add(id="PLS", content="Купить пластыри", due={"date": "2026-09-23"})
    c.todoist.add(id="HAIR", content="Постричься", due={"date": "2026-09-24"})
    c.todoist.add(id="MILK", content="Купить молоко", due={"date": "2026-09-23"})  # not covered by any event
    for job in ("todoist.reconcile", "calendar.reconcile"):
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    async with c.db.session() as s:
        await c.planner.propose(s, c.clock.now(), requested_by="test")
        items = {i.todoist_task_id: (i.action, i.reason) for i in (await s.scalars(select(PlanningProposalItem))).all()}
    assert items["VNJ"] == ("skip", "already_in_calendar")
    assert items["PLS"] == ("skip", "already_in_calendar")
    assert items["HAIR"][0] == "schedule"
    assert items["MILK"][0] == "schedule"


def test_covered_by_rules() -> None:
    from jarvis.core.text import covered_by

    assert covered_by("Купить пластыри", "Купить батарейки и пластыри")
    assert covered_by("Постричься", "постричься")
    assert not covered_by("Купить сигару", "Купить батарейки и пластыри")
    assert not covered_by("Барбер", "Найти барбера и записаться")  # one word is too weak a signal


async def test_agenda_hides_tasks_merged_into_a_same_day_event(container: Any) -> None:
    """R15 in the agenda: tasks merged into one calendar event of that day are shown once (as the event)."""
    c = container
    c.calendar.add_event(
        "primary",
        summary="Купить батарейки и пластыри",
        start={"dateTime": iso(local(14, 15))},
        end={"dateTime": iso(local(14, 45))},
    )
    c.todoist.add(id="PLS", content="Купить пластыри", due={"date": "2026-09-23"})
    c.todoist.add(id="PLS2", content="Купить пластыри", due={"date": "2026-09-24"})  # another day: stays visible
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
        await c.calendar_sync.reconcile(s, c.clock.now())
    bot = BotService(c)
    await bot.cmd_agenda(1001, "сегодня и завтра")
    text = c.telegram.texts()[-1]
    assert text.count("Купить пластыри") == 1
    assert "📆 Завтра, чт 24.09\n☐ Купить пластыри" in text


async def _drain_until(c: Any, worker: Any, until: datetime) -> None:
    """Run every job due up to `until`, moving the clock job by job (as the always-on worker would)."""
    while True:
        async with c.db.session() as s:
            next_run = await s.scalar(select(func.min(ScheduledJob.run_at)).where(ScheduledJob.status == "pending"))
        if next_run is None or next_run > until:
            break
        c.clock.set(max(next_run, c.clock.now()))
        await worker.drain()
    c.clock.set(until)


async def test_task_completed_in_todoist_app_silences_its_calendar_event(container: Any, worker: Any) -> None:
    """R18 (found live 2026-09-23): «Заказать цветы для девушки» was completed in the Todoist app at 15:32, yet the
    calendar event with the same title reminded at 15:45 — the event reminder never looked at the linked task, and
    the cache learned about the completion only on the next 10-minute poll."""
    c = container
    c.calendar.add_event(
        "primary",
        summary="Заказать цветы для девушки",
        start={"dateTime": iso(local(10, 0))},
        end={"dateTime": iso(local(10, 30))},
    )
    c.todoist.add(
        id="FLW",
        content="Заказать цветы для девушки",
        due={"date": "2026-09-23", "datetime": iso(local(10, 0)), "is_recurring": False},
    )
    for job in ("todoist.reconcile", "calendar.reconcile"):  # calendar.reconcile also plans reminders
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    async with c.db.session() as s:
        scheduled = (await s.scalars(select(Reminder).where(Reminder.status == "scheduled"))).all()
    assert scheduled and all(r.source_type == "calendar_event" for r in scheduled)  # one reminder per item (R11)

    await c.todoist.close_task("FLW", request_id="todoist-app")  # the user taps "done" in the Todoist app
    await _drain_until(c, worker, local(10, 45))  # no Todoist poll in between: only the pre-reminder check
    assert c.telegram.sent == []
    assert c.call_provider.placed == []
    async with c.db.session() as s:
        outcomes = {r.outcome for r in (await s.scalars(select(Reminder))).all()}
    assert "linked Todoist task completed before firing" in outcomes


async def test_merged_event_reminds_until_all_its_tasks_are_done(container: Any, worker: Any) -> None:
    """R18: one event «Купить батарейки и пластыри» for two tasks — still reminds while one of them is open."""
    c = container
    c.settings.voice_calls_enabled = False
    c.calendar.add_event(
        "primary",
        summary="Купить батарейки и пластыри",
        start={"dateTime": iso(local(12, 0))},
        end={"dateTime": iso(local(12, 30))},
    )
    c.todoist.add(id="TAD", content="Купить батарейки", due={"date": "2026-09-23"})
    c.todoist.add(id="PLS", content="Купить пластыри", due={"date": "2026-09-23"})
    for job in ("todoist.reconcile", "calendar.reconcile"):
        async with c.db.session() as s:
            await enqueue(s, job_type=job, run_at=c.clock.now())
        await worker.drain()
    await c.todoist.close_task("PLS", request_id="app")
    await _drain_until(c, worker, local(11, 45))  # the 11:30 reminder: one task still open → it is sent
    assert any("Купить батарейки и пластыри" in t for t in c.telegram.texts())
    sent_before = len(c.telegram.sent)
    await c.todoist.close_task("TAD", request_id="app")
    await _drain_until(c, worker, local(12, 45))
    assert len(c.telegram.sent) == sent_before  # both done → the remaining reminders are silent


async def test_today_moves_items_completed_elsewhere_to_done(container: Any) -> None:
    """R18 in the chat: /today pulls Todoist when the cache is older than a minute and shows the completed item as
    done instead of listing its calendar event."""
    c = container
    c.calendar.add_event(
        "primary", summary="Снять 2 000 $", start={"dateTime": iso(local(13, 0))}, end={"dateTime": iso(local(14, 0))}
    )
    c.todoist.add(id="CASH", content="Снять 2 000 $", due={"date": "2026-09-23", "datetime": iso(local(13, 0))})
    c.todoist.add(id="PILL", content="Купить пластыри", due={"date": "2026-09-23"})
    async with c.db.session() as s:
        await c.todoist_sync.reconcile(s, c.clock.now())
        await c.calendar_sync.reconcile(s, c.clock.now())
    await c.todoist.close_task("CASH", request_id="chatgpt")  # completed via ChatGPT's Todoist connector
    await c.todoist.close_task("PILL", request_id="app")
    c.clock.advance(timedelta(minutes=3))  # the cache is now older than a minute
    bot = BotService(c)
    await bot.cmd_today(1001)
    text = c.telegram.texts()[-1]
    assert "• 13:00–14:00 Снять 2 000 $" not in text
    assert "✅ Уже сделано: Снять 2 000 $" in text
    assert "Купить пластыри" not in text


def test_todoist_polls_every_two_minutes() -> None:
    from tests.conftest import make_settings

    assert make_settings().todoist_reconcile_minutes == 2
