"""Planner orchestration: DB → PlanningTasks/Busy → engine → proposal → (approval) → apply to Todoist."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.approvals.service import request_approval
from jarvis.audit.service import A, audit
from jarvis.core.clock import format_local, local_datetime, parse_hhmm, to_local
from jarvis.core.config import AutoApplyPolicy, Settings
from jarvis.core.errors import PermanentError
from jarvis.core.text import covered_by, same_item_titles
from jarvis.db.models import (
    CalendarEvent,
    PlannerRun,
    PlanningProposal,
    PlanningProposalItem,
    Reminder,
    TodoistTask,
)
from jarvis.integrations.todoist.client import MAX_DURATION_MINUTES, TodoistApi
from jarvis.planner.constraints import parse_preferences_keywords, parse_splittable
from jarvis.planner.engine import BusyBlock, Interval, PlannerConfig, PlanningTask, plan
from jarvis.scheduler.queue import enqueue

log = logging.getLogger(__name__)


@dataclass
class ProposalOutcome:
    proposal_id: uuid.UUID | None
    text: str
    approval_nonce: str | None
    auto_applied: bool
    scheduled: int
    unschedulable: int


def _end_of_local_day(day: date, settings: Settings) -> datetime:
    return local_datetime(day, parse_hhmm(settings.working_hours_end), settings.tz)


def task_to_planning(task: TodoistTask, settings: Settings, today: date | None = None) -> PlanningTask:
    latest: datetime | None = None
    if task.due_date is not None and task.due_datetime is None:
        due_day = max(task.due_date, today) if today else task.due_date  # overdue → plan for today
        latest = _end_of_local_day(due_day, settings)
    deadline = None
    if task.deadline_date is not None:
        deadline = local_datetime(task.deadline_date, time(23, 59), settings.tz)
    hint = " ".join([*task.labels, task.description or ""])
    duration = task.duration_minutes or settings.planner_default_duration_minutes
    return PlanningTask(
        task_id=task.id,
        title=task.content,
        duration_minutes=min(duration, MAX_DURATION_MINUTES),
        priority=task.priority,
        latest_finish=latest,
        hard_deadline=deadline,
        preferred_periods=parse_preferences_keywords(hint),
        splittable=parse_splittable(hint),
        assumed_duration=task.duration_minutes is None,
    )


async def load_busy(
    session: AsyncSession, start: datetime, end: datetime, *, unknown_travel_minutes: int = 30
) -> list[BusyBlock]:
    events = (
        await session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.status != "cancelled",
                CalendarEvent.transparent.is_(False),
                CalendarEvent.self_declined.is_(False),
                CalendarEvent.jarvis_managed.is_(False),  # mirror events are tasks, not availability
                CalendarEvent.all_day.is_(False),
                CalendarEvent.start_at < end,
                CalendarEvent.end_at > start,
            )
        )
    ).all()
    # Departure times already computed by the reminder engine (Maps), keyed by event occurrence.
    leave_rows = (
        await session.scalars(
            select(Reminder).where(
                Reminder.source_type == "calendar_event",
                Reminder.kind == "leave",
                Reminder.offset_minutes == 0,
                Reminder.status != "cancelled",
            )
        )
    ).all()
    leave_at = {(r.source_id, r.occurrence_start): r.fire_at for r in leave_rows}
    busy: list[BusyBlock] = []
    for e in events:
        if e.start_at is None or e.end_at is None or e.end_at <= e.start_at:
            continue
        block_start = e.start_at
        if e.location:
            known = leave_at.get((f"{e.calendar_id}:{e.event_id}"[:120], e.start_at))
            # Travel to an event with an address is busy time too. Unknown travel → conservative block
            # (this only reduces availability; reminders never use this guess).
            block_start = known if known is not None else e.start_at - timedelta(minutes=unknown_travel_minutes)
        busy.append(BusyBlock(Interval(min(block_start, e.start_at), e.end_at), e.summary or "event"))
    # Already time-blocked Todoist tasks occupy time too (Strategy A: they are NOT read from the
    # Todoist calendar, which is excluded, so there is no double counting).
    timed = (
        await session.scalars(
            select(TodoistTask).where(
                TodoistTask.checked.is_(False),
                TodoistTask.is_deleted.is_(False),
                TodoistTask.due_datetime.is_not(None),
                TodoistTask.due_datetime < end,
            )
        )
    ).all()
    for task in timed:
        assert task.due_datetime is not None
        minutes = task.duration_minutes or 0
        if minutes <= 0:
            continue
        interval = Interval(task.due_datetime, task.due_datetime + timedelta(minutes=minutes))
        if interval.end > start:
            busy.append(BusyBlock(interval, f"task:{task.id}", hard=False))
    return busy


class PlannerService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def config(self, now: datetime, days: int | None = None) -> PlannerConfig:
        s = self.settings
        horizon_end = local_datetime(
            to_local(now, s.tz).date() + timedelta(days=(days or s.planner_horizon_days)), time(0, 0), s.tz
        )
        return PlannerConfig(
            tz=s.tz,
            # Never propose a block that starts right now: leave one grid step to react.
            horizon_start=now + timedelta(minutes=s.planner_slot_granularity_minutes),
            horizon_end=horizon_end,
            work_start=parse_hhmm(s.working_hours_start),
            work_end=parse_hhmm(s.working_hours_end),
            working_days=tuple(s.working_days),
            sleep_start=parse_hhmm(s.sleep_start),
            sleep_end=parse_hhmm(s.sleep_end),
            min_break_minutes=s.planner_min_break_minutes,
            granularity_minutes=s.planner_slot_granularity_minutes,
        )

    async def candidate_tasks(
        self, session: AsyncSession, cfg: PlannerConfig
    ) -> tuple[list[TodoistTask], list[TodoistTask]]:
        last_day = to_local(cfg.horizon_end, cfg.tz).date()
        rows = (
            await session.scalars(
                select(TodoistTask).where(
                    TodoistTask.checked.is_(False),
                    TodoistTask.is_deleted.is_(False),
                    TodoistTask.due_datetime.is_(None),
                    or_(
                        and_(TodoistTask.due_date.is_not(None), TodoistTask.due_date <= last_day),
                        and_(TodoistTask.deadline_date.is_not(None), TodoistTask.deadline_date <= last_day),
                    ),
                )
            )
        ).all()
        plannable = [t for t in rows if not t.due_is_recurring]
        skipped = [t for t in rows if t.due_is_recurring]
        return plannable, skipped

    async def propose(self, session: AsyncSession, now: datetime, requested_by: str) -> ProposalOutcome:
        cfg = self.config(now)
        tasks, recurring = await self.candidate_tasks(session, cfg)
        # A task the user already placed in the calendar is not planned again — also when the event is beyond the
        # planning horizon (found live: an event 3 days ahead) or merges several tasks («Купить батарейки и
        # пластыри» covers «Купить пластыри» on the same day).
        placed = (
            await session.execute(
                select(CalendarEvent.summary, CalendarEvent.start_at, CalendarEvent.end_at).where(
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.jarvis_managed.is_(False),
                    CalendarEvent.start_at >= now - timedelta(days=1),
                )
            )
        ).all()

        def already_in_calendar(task: TodoistTask) -> bool:
            for summary, start_at, end_at in placed:
                if start_at is None:
                    continue
                same_day = task.due_date == to_local(start_at, cfg.tz).date()
                upcoming = end_at is not None and end_at > now
                if (upcoming or same_day) and same_item_titles(task.content, summary):
                    return True
                if same_day and covered_by(task.content, summary):
                    return True
            return False

        in_calendar = [t for t in tasks if already_in_calendar(t)]
        tasks = [t for t in tasks if t not in in_calendar]
        busy = await load_busy(
            session,
            cfg.horizon_start,
            cfg.horizon_end,
            unknown_travel_minutes=self.settings.planner_unknown_travel_minutes,
        )
        today = to_local(now, self.settings.tz).date()
        planning_tasks = [task_to_planning(t, self.settings, today) for t in tasks]
        result = plan(planning_tasks, [b for b in busy], cfg)

        run = PlannerRun(
            requested_by=requested_by,
            status="succeeded",
            horizon_start=cfg.horizon_start,
            horizon_end=cfg.horizon_end,
            input_summary={"tasks": len(tasks), "busy": len(busy), "recurring_skipped": len(recurring)},
        )
        session.add(run)
        await session.flush()
        if not result.placements and not result.unschedulable and not in_calendar:
            return ProposalOutcome(
                None, "Нечего планировать: нет задач без времени на ближайшие дни.", None, False, 0, 0
            )
        proposal = PlanningProposal(run_id=run.id, status="pending")
        session.add(proposal)
        await session.flush()
        by_id = {t.id: t for t in tasks}
        pt_by_id = {p.task_id: p for p in planning_tasks}
        lines = ["🗓 Предложение по расписанию:"]
        for placement in result.placements:
            task = by_id[placement.task_id]
            session.add(
                PlanningProposalItem(
                    proposal_id=proposal.id,
                    todoist_task_id=task.id,
                    action="schedule",
                    start_at=placement.interval.start,
                    end_at=placement.interval.end,
                    duration_minutes=int(placement.interval.minutes),
                    reason=",".join(placement.reasons),
                    previous={
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                        "duration_minutes": task.duration_minutes,
                    },
                )
            )
            assumed = " (длительность по умолчанию)" if pt_by_id[task.id].assumed_duration else ""
            lines.append(
                f"• {format_local(placement.interval.start, cfg.tz, '%d.%m %H:%M')}–"
                f"{format_local(placement.interval.end, cfg.tz, '%H:%M')} {task.content}{assumed}"
            )
        reasons_ru = {
            "no_slot_long_enough": "нет достаточно длинного окна",
            "no_slot_before_deadline": "нет окна до дедлайна",
            "needs_split": "нужно разбить на части (Todoist хранит один блок на задачу)",
            "invalid_duration": "некорректная длительность",
        }
        for task_id, reason in result.unschedulable.items():
            session.add(
                PlanningProposalItem(
                    proposal_id=proposal.id, todoist_task_id=task_id, action="unschedulable", reason=reason
                )
            )
            lines.append(f"⚠️ {by_id[task_id].content}: {reasons_ru.get(reason, reason)}")
        for task in in_calendar:
            session.add(
                PlanningProposalItem(
                    proposal_id=proposal.id, todoist_task_id=task.id, action="skip", reason="already_in_calendar"
                )
            )
        if in_calendar:
            lines.append(f"ℹ️ Уже стоят в календаре, не трогаю: {len(in_calendar)}")
        for task in recurring:
            session.add(
                PlanningProposalItem(
                    proposal_id=proposal.id, todoist_task_id=task.id, action="skip", reason="recurring"
                )
            )
        if recurring:
            lines.append(f"ℹ️ Повторяющиеся задачи не переносятся автоматически: {len(recurring)}")
        proposal.summary = "\n".join(lines)
        policy = self.settings.planner_auto_apply
        eligible = bool(result.placements) and (
            policy == AutoApplyPolicy.ALWAYS
            or (
                policy == AutoApplyPolicy.SMALL
                and len(result.placements) <= self.settings.planner_small_change_max_tasks
            )
        )
        proposal.auto_apply_eligible = eligible
        await audit(
            session,
            now=now,
            actor=requested_by,
            action=A.PLAN_PROPOSED,
            target_type="proposal",
            target_id=str(proposal.id),
            details={"placements": len(result.placements), "unschedulable": len(result.unschedulable)},
        )
        nonce = None
        if eligible:
            proposal.status = "approved"
            await enqueue(
                session,
                job_type="planner.apply",
                run_at=now,
                payload={"proposal_id": str(proposal.id)},
                dedupe_key=f"planner.apply:{proposal.id}",
            )
            proposal.summary += "\n\n✅ Применяется автоматически (политика auto-apply)."
        elif result.placements:
            approval = await request_approval(
                session,
                kind="plan_apply",
                target_type="planning_proposal",
                target_id=str(proposal.id),
                now=now,
                ttl=timedelta(hours=12),
            )
            proposal.approval_id = approval.id
            nonce = approval.nonce
        return ProposalOutcome(
            proposal.id, proposal.summary, nonce, eligible, len(result.placements), len(result.unschedulable)
        )

    async def approve(self, session: AsyncSession, proposal_id: uuid.UUID, now: datetime) -> None:
        proposal = await session.get(PlanningProposal, proposal_id)
        if proposal is None or proposal.status != "pending":
            return
        proposal.status = "approved"
        await enqueue(
            session,
            job_type="planner.apply",
            run_at=now,
            payload={"proposal_id": str(proposal_id)},
            dedupe_key=f"planner.apply:{proposal_id}",
        )

    async def apply(
        self, session: AsyncSession, api: TodoistApi, proposal_id: uuid.UUID, now: datetime
    ) -> dict[str, int]:
        """Idempotent: items already applied are skipped; tasks already at the target are not rewritten."""
        proposal = await session.get(PlanningProposal, proposal_id)
        if proposal is None or proposal.status not in ("approved", "partially_applied"):
            return {"skipped": 1}
        items = (
            await session.scalars(
                select(PlanningProposalItem).where(
                    PlanningProposalItem.proposal_id == proposal_id,
                    PlanningProposalItem.action == "schedule",
                    PlanningProposalItem.apply_status != "applied",
                )
            )
        ).all()
        applied = failed = 0
        for item in items:
            task = await session.get(TodoistTask, item.todoist_task_id)
            if task is None or task.checked or task.is_deleted or task.due_is_recurring:
                item.apply_status = "skipped"
                item.apply_error = "task changed since proposal"
                continue
            assert item.start_at is not None
            assert item.duration_minutes is not None
            if item.start_at <= now:
                item.apply_status = "skipped"
                item.apply_error = "slot already in the past; run /plan again"
                continue
            if task.due_datetime == item.start_at and task.duration_minutes == item.duration_minutes:
                item.apply_status = "applied"
                item.applied_at = now
                continue
            if task.due_datetime is not None:
                item.apply_status = "skipped"
                item.apply_error = "task got a fixed time meanwhile"
                continue
            try:
                await api.schedule_task(task.id, item.start_at, item.duration_minutes, request_id=str(item.id))
            except PermanentError as exc:
                # TransientError propagates: the job retries the whole (idempotent) apply.
                item.apply_status = "failed"
                item.apply_error = str(exc)[:500]
                failed += 1
                continue
            task.due_datetime = item.start_at
            task.duration_minutes = item.duration_minutes
            item.apply_status = "applied"
            item.applied_at = now
            applied += 1
            await audit(
                session,
                now=now,
                actor="planner",
                action=A.TASK_UPDATED,
                target_type="todoist_task",
                target_id=task.id,
                details={"start": item.start_at.isoformat(), "duration": item.duration_minutes},
            )
        proposal.status = "partially_applied" if failed else "applied"
        proposal.applied_at = now
        await audit(
            session,
            now=now,
            actor="planner",
            action=A.PLAN_APPLIED,
            target_type="proposal",
            target_id=str(proposal_id),
            details={"applied": applied, "failed": failed},
        )
        return {"applied": applied, "failed": failed}
