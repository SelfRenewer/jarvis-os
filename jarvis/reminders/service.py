"""Reminder planning and execution.

`plan_upcoming` is idempotent: it computes the desired reminder set for each upcoming event and
diffs it against stored reminders (keyed by source+occurrence+kind+offset+channel):
* missing → created with a durable job;
* scheduled but anchor moved (traffic refresh) → job rescheduled;
* scheduled but no longer desired (event moved/cancelled/location changed) → cancelled;
* already sent → never recreated (no duplicate reminders).
`fire` re-validates everything at execution time (event still exists, same start, grace windows).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.service import A, audit
from jarvis.core.clock import format_local
from jarvis.core.config import Settings
from jarvis.core.text import same_item_titles
from jarvis.db.models import CalendarEvent, Reminder, TodoistTask
from jarvis.integrations.maps.routes import RouteStatus
from jarvis.reminders.policy import (
    Channel,
    Kind,
    LateAction,
    ReminderPolicy,
    compute_leave_at,
    decide_late_action,
    minutes_between,
    plan_reminders,
)
from jarvis.reminders.travel import DEPARTURE_LEAD, TravelInfo, TravelService
from jarvis.scheduler.queue import cancel_job, enqueue, reschedule_job
from jarvis.sync.links import LINK_TOLERANCE, done_by_tasks, linked_tasks, tasks_for_linking

log = logging.getLogger(__name__)

SOURCE_EVENT = "calendar_event"
SOURCE_TASK = "todoist_task"
# A timed task and a calendar event with the same title within this distance are the same item.
# Grace windows are re-evaluated on every attempt, so retrying for ~20 min (outages) is safe.
REMINDER_FIRE_ATTEMPTS = 8


def policy_from_settings(settings: Settings) -> ReminderPolicy:
    return ReminderPolicy(
        leave_offsets_minutes=tuple(settings.reminder_offsets_minutes),
        call_offsets_minutes=tuple(settings.reminder_call_offsets_minutes),
        travel_buffer_minutes=settings.reminder_travel_buffer_minutes,
        calls_enabled=settings.voice_calls_enabled and bool(settings.user_phone_number),
    )


def source_id_of(event: CalendarEvent) -> str:
    return f"{event.calendar_id}:{event.event_id}"[:120]


def reminder_key(source_id: str, occurrence: datetime, kind: str, offset: int, channel: str) -> str:
    return f"rem:{source_id}:{occurrence.isoformat()}:{kind}:{offset}:{channel}"[:300]


@dataclass
class PlanStats:
    events: int = 0
    created: int = 0
    rescheduled: int = 0
    cancelled: int = 0
    travel_queries: int = 0


class ReminderService:
    def __init__(self, settings: Settings, travel: TravelService) -> None:
        self.settings = settings
        self.travel = travel
        self.policy = policy_from_settings(settings)

    async def upcoming_events(self, session: AsyncSession, now: datetime) -> list[CalendarEvent]:
        horizon = now + timedelta(hours=self.settings.reminder_horizon_hours)
        return list(
            (
                await session.scalars(
                    select(CalendarEvent)
                    .where(
                        CalendarEvent.status != "cancelled",
                        CalendarEvent.all_day.is_(False),
                        CalendarEvent.self_declined.is_(False),
                        CalendarEvent.jarvis_managed.is_(False),
                        CalendarEvent.start_at > now,
                        CalendarEvent.start_at <= horizon,
                    )
                    .order_by(CalendarEvent.start_at)
                )
            ).all()
        )

    async def travel_for(
        self, session: AsyncSession, event: CalendarEvent, now: datetime, *, force_refresh: bool = False
    ) -> TravelInfo | None:
        if not event.location or event.start_at is None:
            return None
        origin = await self.travel.resolve_origin(session, event, now=now)
        if not origin:
            return TravelInfo(RouteStatus.UNAVAILABLE, None, None, False, None, "no origin configured")
        # Departure ≈ start − 60 min for the traffic model. It depends only on the occurrence (not on `now`),
        # so periodic re-planning hits the cache; the single pre-departure refresh job forces a new query.
        departure = event.start_at - DEPARTURE_LEAD
        return await self.travel.estimate(
            session,
            origin=origin,
            destination=event.location,
            departure=departure,
            now=now,
            force_refresh=force_refresh,
        )

    async def plan_event(
        self,
        session: AsyncSession,
        event: CalendarEvent,
        now: datetime,
        stats: PlanStats,
        *,
        force_travel_refresh: bool = False,
        known: list[Reminder] | None = None,
    ) -> None:
        """`known`: this source's reminders, read beforehand for every event at once (`plan_upcoming`)."""
        assert event.start_at is not None
        info = await self.travel_for(session, event, now, force_refresh=force_travel_refresh)
        if info is not None:
            stats.travel_queries += 1
        leave_at = None
        if info is not None and info.status == RouteStatus.OK and info.duration_seconds is not None:
            leave_at = compute_leave_at(event.start_at, info.duration_seconds, self.policy.travel_buffer_minutes)
        elif info is not None and info.status in (RouteStatus.QUOTA, RouteStatus.UNAVAILABLE):
            # Routing temporarily unavailable: keep the last computed departure for this occurrence
            # instead of flipping already-scheduled "leave" reminders (no churn, no invented times).
            previous = await session.scalar(
                select(Reminder).where(
                    Reminder.source_type == SOURCE_EVENT,
                    Reminder.source_id == source_id_of(event),
                    Reminder.occurrence_start == event.start_at,
                    Reminder.kind == Kind.LEAVE.value,
                )
            )
            if previous is not None and previous.payload.get("leave_at"):
                leave_at = datetime.fromisoformat(previous.payload["leave_at"])
        desired = plan_reminders(
            event_start=event.start_at, leave_at=leave_at, has_location=bool(event.location), policy=self.policy
        )
        source_id = source_id_of(event)
        if known is None:
            known = list((await self._reminders_of(session, SOURCE_EVENT, [source_id])).get(source_id, []))
        existing = {r.dedupe_key: r for r in known}
        desired_keys: set[str] = set()
        grace = timedelta(seconds=self.settings.telegram_grace_seconds)
        for item in desired:
            key = reminder_key(source_id, event.start_at, item.kind, item.offset_minutes, item.channel)
            desired_keys.add(key)
            payload = {
                "event_uuid": str(event.id),
                "summary": event.summary,
                "location": event.location,
                "leave_at": leave_at.isoformat() if leave_at else None,
                "travel_seconds": info.duration_seconds if info else None,
                "travel_status": info.status.value if info else None,
                "origin": info.origin if info else None,
            }
            current = existing.get(key)
            if current is not None:
                if current.status == "scheduled" and current.fire_at != item.fire_at:
                    current.fire_at = item.fire_at
                    current.payload = payload
                    if current.job_id:
                        await reschedule_job(session, current.job_id, item.fire_at)
                    stats.rescheduled += 1
                continue
            if item.fire_at < now - grace:
                continue  # newly discovered but already long past: don't spam retroactively
            reminder = Reminder(
                source_type=SOURCE_EVENT,
                source_id=source_id,
                occurrence_start=event.start_at,
                kind=item.kind.value,
                offset_minutes=item.offset_minutes,
                channel=item.channel.value,
                fire_at=item.fire_at,
                status="scheduled",
                dedupe_key=key,
                payload=payload,
            )
            session.add(reminder)
            known.append(reminder)
            await session.flush()
            reminder.job_id = await enqueue(
                session,
                job_type="reminder.fire",
                run_at=item.fire_at,
                payload={"reminder_id": str(reminder.id)},
                dedupe_key=f"reminder.fire:{reminder.id}",
                priority=10,
                max_attempts=REMINDER_FIRE_ATTEMPTS,
            )
            stats.created += 1
            await audit(
                session,
                now=now,
                actor="reminders",
                action=A.REMINDER_SCHEDULED,
                target_type="reminder",
                target_id=str(reminder.id),
                details={"fire_at": item.fire_at.isoformat(), "channel": item.channel.value},
            )
        for key, reminder in existing.items():
            if key not in desired_keys and reminder.status == "scheduled":
                reminder.status = "cancelled"
                reminder.outcome = "superseded (event changed)"
                if reminder.job_id:
                    await cancel_job(session, reminder.job_id, "reminder superseded")
                stats.cancelled += 1
                await audit(
                    session,
                    now=now,
                    actor="reminders",
                    action=A.REMINDER_CANCELLED,
                    target_type="reminder",
                    target_id=str(reminder.id),
                )
        # One traffic-aware refresh shortly before departure (bounded: one per occurrence).
        if leave_at is not None and info is not None and info.traffic_aware:
            refresh_at = leave_at - timedelta(minutes=self.settings.travel_refresh_lead_minutes)
            if refresh_at > now:
                await enqueue(
                    session,
                    job_type="reminders.refresh_travel",
                    run_at=refresh_at,
                    payload={"event_uuid": str(event.id)},
                    dedupe_key=f"travel-refresh:{source_id}:{event.start_at.isoformat()}",
                    priority=20,
                )

    async def _cancel_event_reminders(
        self, session: AsyncSession, event: CalendarEvent, outcome: str, stats: PlanStats
    ) -> None:
        scheduled = (
            await session.scalars(
                select(Reminder).where(
                    Reminder.source_type == SOURCE_EVENT,
                    Reminder.source_id == source_id_of(event),
                    Reminder.status == "scheduled",
                )
            )
        ).all()
        for reminder in scheduled:
            reminder.status = "cancelled"
            reminder.outcome = outcome
            if reminder.job_id:
                await cancel_job(session, reminder.job_id, outcome)
            stats.cancelled += 1

    async def plan_task(
        self,
        session: AsyncSession,
        task: TodoistTask,
        now: datetime,
        stats: PlanStats,
        *,
        covered: bool = False,
        known: list[Reminder] | None = None,
    ) -> None:
        """Timed Todoist tasks get a Telegram reminder at their time (no travel logic).

        `covered`: the same item also exists as a calendar event at (about) the same time — the event's reminder
        is enough, so the task gets none (and any scheduled ones are cancelled).
        """
        assert task.due_datetime is not None
        if known is None:
            known = list((await self._reminders_of(session, SOURCE_TASK, [task.id])).get(task.id, []))
        existing = {r.dedupe_key: r for r in known}
        desired: set[str] = set()
        grace = timedelta(seconds=self.settings.telegram_grace_seconds)
        offsets = [] if covered else sorted(set(self.settings.reminder_task_offsets_minutes), reverse=True)
        for offset in offsets:
            fire_at = task.due_datetime - timedelta(minutes=offset)
            key = reminder_key(f"task:{task.id}", task.due_datetime, Kind.START, offset, Channel.TELEGRAM)
            desired.add(key)
            if key in existing or fire_at < now - grace:
                continue
            reminder = Reminder(
                source_type=SOURCE_TASK,
                source_id=task.id,
                occurrence_start=task.due_datetime,
                kind=Kind.START.value,
                offset_minutes=offset,
                channel=Channel.TELEGRAM.value,
                fire_at=fire_at,
                status="scheduled",
                dedupe_key=key,
                payload={"task_id": task.id, "summary": task.content},
            )
            session.add(reminder)
            known.append(reminder)
            await session.flush()
            reminder.job_id = await enqueue(
                session,
                job_type="reminder.fire",
                run_at=fire_at,
                payload={"reminder_id": str(reminder.id)},
                dedupe_key=f"reminder.fire:{reminder.id}",
                priority=10,
                max_attempts=REMINDER_FIRE_ATTEMPTS,
            )
            stats.created += 1
        for key, reminder in existing.items():
            if key not in desired and reminder.status == "scheduled":
                reminder.status = "cancelled"
                reminder.outcome = "covered by a calendar event" if covered else "superseded (task changed)"
                if reminder.job_id:
                    await cancel_job(session, reminder.job_id, "reminder superseded")
                stats.cancelled += 1

    async def _reminders_of(self, session: AsyncSession, source_type: str, ids: list[str]) -> dict[str, list[Reminder]]:
        out: dict[str, list[Reminder]] = {}
        if ids:
            rows = await session.scalars(
                select(Reminder).where(Reminder.source_type == source_type, Reminder.source_id.in_(ids))
            )
            for r in rows.all():
                out.setdefault(r.source_id, []).append(r)
        return out

    async def plan_upcoming(self, session: AsyncSession, now: datetime) -> PlanStats:
        """Every five minutes. Reminders are read once for all events and once for all tasks, not per item (L12)."""
        stats = PlanStats()
        events = await self.upcoming_events(session, now)
        active_sources = {source_id_of(e) for e in events}
        by_event = await self._reminders_of(session, SOURCE_EVENT, list(active_sources))
        horizon = now + timedelta(hours=self.settings.reminder_horizon_hours)
        link_tasks = await tasks_for_linking(session, now, horizon, self.settings.tz)
        for event in events:
            stats.events += 1
            if done_by_tasks(event, link_tasks, self.settings.tz):
                # The same item was completed in Todoist (app, ChatGPT, bot): the event must not remind any more.
                await self._cancel_event_reminders(session, event, "linked Todoist task completed", stats)
                continue
            await self.plan_event(session, event, now, stats, known=by_event.setdefault(source_id_of(event), []))
        tasks = (
            await session.scalars(
                select(TodoistTask).where(
                    TodoistTask.checked.is_(False),
                    TodoistTask.is_deleted.is_(False),
                    TodoistTask.due_datetime > now,
                    TodoistTask.due_datetime <= horizon,
                )
            )
        ).all()
        timed_events = [(e.summary, e.start_at) for e in events if e.start_at is not None]
        by_task = await self._reminders_of(session, SOURCE_TASK, [t.id for t in tasks])
        for task in tasks:
            stats.events += 1
            assert task.due_datetime is not None
            due = task.due_datetime
            covered = any(
                abs(start - due) <= LINK_TOLERANCE and same_item_titles(summary, task.content)
                for summary, start in timed_events
            )
            await self.plan_task(session, task, now, stats, covered=covered, known=by_task.setdefault(task.id, []))
        active_task_ids = {t.id for t in tasks}
        stale_task_reminders = (
            await session.scalars(
                select(Reminder).where(
                    Reminder.status == "scheduled", Reminder.source_type == SOURCE_TASK, Reminder.occurrence_start > now
                )
            )
        ).all()
        for reminder in stale_task_reminders:
            if reminder.source_id not in active_task_ids:
                reminder.status = "cancelled"
                reminder.outcome = "task no longer upcoming"
                if reminder.job_id:
                    await cancel_job(session, reminder.job_id, "task removed")
                stats.cancelled += 1
        # Cancel scheduled reminders whose event disappeared from the upcoming set (cancelled/moved away).
        orphans = (
            await session.scalars(
                select(Reminder).where(
                    Reminder.status == "scheduled",
                    Reminder.source_type == SOURCE_EVENT,
                    Reminder.occurrence_start > now,
                )
            )
        ).all()
        for reminder in orphans:
            if reminder.source_id not in active_sources:
                reminder.status = "cancelled"
                reminder.outcome = "event no longer upcoming"
                if reminder.job_id:
                    await cancel_job(session, reminder.job_id, "event removed")
                stats.cancelled += 1
        return stats


# --- message texts -----------------------------------------------------------------------------------


def leave_text(summary: str, start_local: str, minutes: int, destination: str | None) -> str:
    where = f"\n📍 {destination}" if destination else ""
    if minutes <= 0:
        return f"🚶 Пора выходить. «{summary}» начинается в {start_local}.{where}"
    return f"⏰ Через {minutes} мин нужно выходить. «{summary}» в {start_local}.{where}"


def start_text(summary: str, start_local: str, minutes: int, travel_note: str | None) -> str:
    note = f"\n⚠️ {travel_note}" if travel_note else ""
    return f"⏰ Через {minutes} мин: «{summary}» ({start_local}).{note}"


@dataclass
class FireDecision:
    action: LateAction
    reminder: Reminder
    event: CalendarEvent | None
    text: str
    variables: dict[str, str]


async def build_fire_decision(
    session: AsyncSession, reminder_id: uuid.UUID, now: datetime, settings: Settings
) -> FireDecision | None:
    reminder = await session.get(Reminder, reminder_id, with_for_update=True)
    # "firing" = a previous attempt crashed after deciding; delivery layers are idempotent.
    if reminder is None or reminder.status not in ("scheduled", "firing"):
        return None
    if reminder.source_type == SOURCE_TASK:
        return await _task_fire_decision(session, reminder, now, settings)
    event = await session.get(CalendarEvent, uuid.UUID(reminder.payload["event_uuid"]))
    if event is None or event.status == "cancelled" or event.start_at != reminder.occurrence_start:
        reminder.status = "cancelled"
        reminder.outcome = "event cancelled or moved before firing"
        return FireDecision(LateAction.MISSED, reminder, None, "", {})
    if done_by_tasks(event, await linked_tasks(session, event, settings.tz), settings.tz):
        reminder.status = "cancelled"
        reminder.outcome = "linked Todoist task completed before firing"
        return FireDecision(LateAction.MISSED, reminder, None, "", {})
    tz = settings.tz
    assert event.start_at is not None
    reminder.status = "firing"
    action = decide_late_action(
        now=now,
        fire_at=reminder.fire_at,
        event_start=event.start_at,
        channel=Channel(reminder.channel),
        call_grace=timedelta(seconds=settings.call_grace_seconds),
        telegram_grace=timedelta(seconds=settings.telegram_grace_seconds),
    )
    summary = event.summary or "Событие"
    start_local = format_local(event.start_at, tz)
    leave_at_raw = reminder.payload.get("leave_at")
    variables: dict[str, str] = {
        "event_title": summary,
        "event_start_local": start_local,
        "destination": event.location or "",
        "user_name": settings.default_user_name,
    }
    if reminder.kind == Kind.LEAVE and leave_at_raw:
        leave_at = datetime.fromisoformat(leave_at_raw)
        minutes = minutes_between(leave_at, now)
        text = leave_text(summary, start_local, minutes, event.location)
        variables.update(leave_at_local=format_local(leave_at, tz), minutes_until_leave=str(minutes))
        if action == LateAction.RECOVERY_NOTICE:
            text = (
                f"⚠️ Напоминание запоздало. Выходить нужно было в {format_local(leave_at, tz)}; "
                f"«{summary}» начинается в {start_local}."
            )
    else:
        minutes = minutes_between(event.start_at, now)
        note = None
        if event.location and reminder.payload.get("travel_status") not in (None, "ok"):
            note = "не удалось рассчитать время в пути — проверь дорогу сам"
        text = start_text(summary, start_local, minutes, note)
    if action == LateAction.MISSED:
        text = f"⚠️ Пропущено напоминание: «{summary}» уже началось в {start_local}."
    next_event = await session.scalar(
        select(CalendarEvent)
        .where(
            CalendarEvent.start_at > event.start_at,
            CalendarEvent.status != "cancelled",
            CalendarEvent.all_day.is_(False),
            CalendarEvent.jarvis_managed.is_(False),
        )
        .order_by(CalendarEvent.start_at)
        .limit(1)
    )
    if next_event is not None and next_event.start_at is not None:
        variables["next_event"] = f"{next_event.summary or 'событие'} в {format_local(next_event.start_at, tz)}"
    else:
        variables["next_event"] = "нет"
    return FireDecision(action, reminder, event, text, variables)


async def _task_fire_decision(
    session: AsyncSession, reminder: Reminder, now: datetime, settings: Settings
) -> FireDecision:
    task = await session.get(TodoistTask, reminder.source_id)
    if task is None or task.checked or task.is_deleted or task.due_datetime != reminder.occurrence_start:
        reminder.status = "cancelled"
        reminder.outcome = "task completed/moved before firing"
        return FireDecision(LateAction.MISSED, reminder, None, "", {})
    reminder.status = "firing"
    lateness = now - reminder.fire_at
    when = format_local(task.due_datetime, settings.tz)
    if lateness > timedelta(seconds=settings.telegram_grace_seconds):
        text = f"⚠️ Напоминание запоздало: «{task.content}» было на {when}."
        action = LateAction.RECOVERY_NOTICE
    else:
        text = f"🔔 Напоминание: «{task.content}» ({when})."
        action = LateAction.DELIVER
    # A synthetic "event" object is not needed for tasks; reuse the task row for type compatibility.
    return FireDecision(action, reminder, task, text, {})  # type: ignore[arg-type]
