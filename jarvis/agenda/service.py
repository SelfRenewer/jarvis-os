"""Agenda for any day or period, the morning briefing and the evening review.

Everything here is deterministic and reads only the local cache (Todoist tasks + calendar events): dates and
times are computed by code, never by the LLM. A Todoist task that duplicates a calendar event of the same day
(the user keeps many items in both places) is shown once, as the event.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.bot import fmt
from jarvis.core.clock import format_local, local_day_bounds, to_local
from jarvis.core.config import Settings
from jarvis.core.dates import WEEKDAY_NAMES, Period, human_day
from jarvis.core.text import covered_by, same_item_titles
from jarvis.db.models import CalendarEvent, Reminder, TodoistTask
from jarvis.integrations.telegram.client import InlineButton
from jarvis.sync.links import done_by_tasks, same_item, tasks_for_linking

PRIORITY_MARK = {4: "🔴 ", 3: "🟠 ", 2: "🔵 "}  # Todoist API priority 4 = p1 (urgent)
DEADLINE_LOOKAHEAD_DAYS = 3
LATE_DAY_HOUR = 16  # after this local hour today's date-only tasks are suggested too


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 задача, 2 задачи, 5 задач."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def human_minutes(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if rest else f"{hours} ч"


@dataclass
class DayView:
    day: date
    events: list[CalendarEvent] = field(default_factory=list)
    tasks: list[TodoistTask] = field(default_factory=list)
    overdue: list[TodoistTask] = field(default_factory=list)
    deadlines: list[TodoistTask] = field(default_factory=list)
    done_events: list[CalendarEvent] = field(default_factory=list)  # their Todoist task(s) are completed

    @property
    def empty(self) -> bool:
        return not (self.events or self.tasks or self.overdue or self.deadlines or self.done_events)


@dataclass
class Attention:
    """A task worth acting on now, with the reason shown to the user."""

    task: TodoistTask
    reason: str  # overdue | missed | soon
    note: str


MOVE_HINT = "➡️ Перенести — скажите, например: «стрижку на пятницу в 18» или «на 2 часа позже»."


def attention_block(items: list[Attention]) -> tuple[list[fmt.Html], list[list[InlineButton]]]:
    """HTML lines + one «✅ <task>» (complete) button per task; moving is said in words (no date buttons).

    The buttons carry the Todoist task id Jarvis itself read from its cache (never from the LLM)."""
    if not items:
        return [], []
    lines = [
        fmt.head("Требует внимания", "💡"),
        *(fmt.bullet(f"«{a.task.content}»", a.note) for a in items),
        fmt.hint(MOVE_HINT),
    ]
    rows = []
    for a in items:
        title = a.task.content if len(a.task.content) <= 26 else a.task.content[:25] + "…"
        rows.append([InlineButton(f"✅ {title}", f"sd:{a.task.id}")])
    return lines, rows


@dataclass
class EveningReview:
    text: fmt.Html
    to_move: list[TodoistTask]
    likely_done: list[TodoistTask]


@dataclass
class MorningBrief:
    """The agenda part of the 08:00 briefing; the job adds weather, pills, suggestions, shopping and the plan."""

    greeting: fmt.Html
    day: fmt.Html
    deadlines: fmt.Html | None
    leave: str | None
    tomorrow: fmt.Html
    note: fmt.Html | None

    def text(self) -> fmt.Html:
        return fmt.blocks(self.greeting, self.day, self.deadlines, self.leave, self.tomorrow, self.note)


def _task_sort_key(task: TodoistTask) -> tuple[int, float, int, str]:
    # Timed tasks first (by time), then date-only ones by priority.
    if task.due_datetime is not None:
        return (0, task.due_datetime.timestamp(), -task.priority, task.content)
    return (1, 0.0, -task.priority, task.content)


def calendar_connected(status: dict[str, str]) -> bool:
    return status.get("google_calendar", "").startswith(("CONFIGURED", "IMPLEMENTED_MOCK_TESTED"))


def duplicates_event(task: TodoistTask, events: Sequence[CalendarEvent]) -> bool:
    """`events` must be the same day's events: then a near-duplicate or covering title is the same item."""
    return any(same_item_titles(task.content, e.summary) or covered_by(task.content, e.summary) for e in events)


class AgendaService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # --- data -------------------------------------------------------------------------------------------

    async def days(self, session: AsyncSession, period: Period, now: datetime) -> list[DayView]:
        tz = self.settings.tz
        today = to_local(now, tz).date()
        range_start, _ = local_day_bounds(period.start, tz)
        _, range_end = local_day_bounds(period.end, tz)
        events = (
            await session.scalars(
                select(CalendarEvent)
                .where(
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.jarvis_managed.is_(False),
                    CalendarEvent.self_declined.is_(False),
                    CalendarEvent.start_at < range_end,
                    CalendarEvent.end_at > range_start,
                )
                .order_by(CalendarEvent.start_at)
            )
        ).all()
        include_overdue = period.start <= today <= period.end
        due_filter = and_(TodoistTask.due_date >= period.start, TodoistTask.due_date <= period.end)
        conditions = [
            due_filter,
            and_(TodoistTask.deadline_date >= period.start, TodoistTask.deadline_date <= period.end),
        ]
        if include_overdue:
            conditions.append(TodoistTask.due_date < today)
        tasks = (
            await session.scalars(
                select(TodoistTask).where(
                    TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False), or_(*conditions)
                )
            )
        ).all()
        link_tasks = await tasks_for_linking(session, range_start, range_end, tz)
        views: list[DayView] = []
        for day in period.days:
            day_start, day_end = local_day_bounds(day, tz)
            day_events = [
                e
                for e in events
                if e.start_at is not None
                and e.end_at is not None
                and e.start_at < day_end
                and (e.end_at > day_start or (e.end_at == e.start_at and e.start_at >= day_start))
            ]
            done = [e for e in day_events if done_by_tasks(e, link_tasks, tz)]
            view = DayView(day, events=[e for e in day_events if e not in done], done_events=done)
            due_today = [t for t in tasks if t.due_date == day]
            view.tasks = sorted((t for t in due_today if not duplicates_event(t, day_events)), key=_task_sort_key)
            if include_overdue and day == today:
                view.overdue = sorted(
                    (t for t in tasks if t.due_date is not None and t.due_date < today),
                    key=lambda t: (t.due_date or today, -t.priority),
                )
            listed = {t.id for t in due_today} | {t.id for t in view.overdue}
            view.deadlines = [t for t in tasks if t.deadline_date == day and t.id not in listed]
            views.append(view)
        return views

    # --- rendering ----------------------------------------------------------------------------------------

    def event_line(self, event: CalendarEvent, day: date) -> fmt.Html:
        """«• <b>18:00–19:00</b> Забрать из аэропорта <i>📍Аэропорт</i>»."""
        tz = self.settings.tz
        assert event.start_at is not None
        assert event.end_at is not None
        day_start, day_end = local_day_bounds(day, tz)
        if event.all_day or (event.start_at <= day_start and event.end_at >= day_end):
            when = "весь день"
        else:
            start = "…" if event.start_at < day_start else format_local(event.start_at, tz)
            end = "…" if event.end_at > day_end else format_local(event.end_at, tz)
            when = start if event.end_at == event.start_at else f"{start}–{end}"
        location = fmt.i(f"📍{event.location}") if event.location else None
        return fmt.line("•", fmt.b(when), event.summary or "Событие", location)

    def task_line(self, task: TodoistTask, *, show_date: bool = False) -> fmt.Html:
        """«☐ <b>10:00</b> 🔴 Заказать цветы 🔁 <i>⏳ дедлайн 25.09</i>»."""
        tz = self.settings.tz
        when = None
        if task.due_datetime is not None:
            when = fmt.b(format_local(task.due_datetime, tz, "%d.%m %H:%M" if show_date else "%H:%M"))
        elif show_date and task.due_date is not None:
            when = fmt.b(task.due_date.strftime("%d.%m"))
        mark = PRIORITY_MARK.get(task.priority, "")
        recurring = " 🔁" if task.due_is_recurring else ""
        deadline = fmt.i(f"⏳ дедлайн {task.deadline_date.strftime('%d.%m')}") if task.deadline_date else None
        return fmt.line("☐", when, f"{mark}{task.content}{recurring}", deadline)

    def day_lines(self, view: DayView) -> fmt.Html:
        """One day: events and tasks, then overdue, deadlines and what is done — groups split by a blank line
        ("" for an empty day)."""
        groups: list[list[fmt.Html]] = [[self.event_line(e, view.day) for e in view.events]]
        groups[0] += [self.task_line(t) for t in view.tasks]
        if view.overdue:
            groups.append(
                [
                    fmt.b(f"⚠️ Просрочено ({len(view.overdue)}):"),
                    *(self.task_line(t, show_date=True) for t in view.overdue),
                ]
            )
        tail = [fmt.line("⏳", fmt.kv("Дедлайн", t.content)) for t in view.deadlines]
        if view.done_events:
            tail.append(fmt.line("✅ Уже сделано:", ", ".join(e.summary or "событие" for e in view.done_events)))
        groups.append(tail)
        return fmt.blocks(*groups)

    def coverage_note(self, period: Period, now: datetime, calendar_connected: bool) -> str | None:
        if not calendar_connected:
            return "ℹ️ Google Calendar не подключён — показываю только задачи Todoist."
        horizon = to_local(now + timedelta(days=self.settings.calendar_sync_days), self.settings.tz).date()
        if period.end > horizon:
            return f"ℹ️ Календарь я вижу до {horizon.strftime('%d.%m')} — дальше показываю только задачи."
        return None

    async def agenda_text(
        self, session: AsyncSession, period: Period, now: datetime, *, calendar_connected: bool = True
    ) -> fmt.Html:
        """The first line is the title (the bot puts the weather right under it); lines never share a tag."""
        today = to_local(now, self.settings.tz).date()
        if period.end < today:
            return fmt.esc(
                f"{human_day(period.end, today)} уже прошло — я показываю сегодняшний день и будущие "
                "(выполненные задачи не храню)."
            )
        clipped = Period(max(period.start, today), period.end)
        views = await self.days(session, clipped, now)
        sections: list[object] = []
        if clipped.single:
            view = views[0]
            sections.append(fmt.title("📅", human_day(view.day, today).capitalize()))
            sections.append(self.day_lines(view) or fmt.hint("Свободный день — ни встреч, ни задач."))
        else:
            sections.append(fmt.title("🗓", f"{clipped.start.strftime('%d.%m')} – {clipped.end.strftime('%d.%m')}"))
            for view in views:
                header = fmt.head(human_day(view.day, today).capitalize(), "📆")
                if view.empty:
                    sections.append(fmt.line(header, fmt.i("— свободно")))
                    continue
                sections.append(fmt.lines(header, self.day_lines(view)))
            events = len({e.id for v in views for e in v.events})
            tasks = sum(len(v.tasks) for v in views)
            sections.append(
                fmt.hint(
                    f"Итого: {plural(events, 'событие', 'события', 'событий')}, "
                    f"{plural(tasks, 'задача', 'задачи', 'задач')}"
                )
            )
        notes: list[object] = []
        if any(v.tasks or v.overdue for v in views) and any(v.events for v in views):
            notes.append(fmt.hint("• — календарь, ☐ — задачи Todoist"))
        if clipped.start > period.start:
            notes.append(fmt.hint("ℹ️ Прошедшие дни пропущены."))
        if note := self.coverage_note(clipped, now, calendar_connected):
            notes.append(fmt.hint(note))
        sections.append(notes)
        return fmt.blocks(*sections)

    # --- attention (proactive suggestions) --------------------------------------------------------------

    async def attention(
        self, session: AsyncSession, now: datetime, *, limit: int = 3, soon_hours: int = 3
    ) -> list[Attention]:
        """Overdue tasks, today's tasks whose time has passed, timed tasks due within `soon_hours`."""
        tz = self.settings.tz
        today = to_local(now, tz).date()
        tasks = (
            await session.scalars(
                select(TodoistTask).where(
                    TodoistTask.checked.is_(False),
                    TodoistTask.is_deleted.is_(False),
                    TodoistTask.due_date <= today + timedelta(days=1),
                )
            )
        ).all()
        overdue = sorted((t for t in tasks if t.due_date and t.due_date < today), key=lambda t: (t.due_date, t.content))
        missed = sorted(
            (t for t in tasks if t.due_date == today and t.due_datetime and t.due_datetime <= now),
            key=lambda t: t.due_datetime or now,
        )
        horizon = now + timedelta(hours=soon_hours)
        day_start, day_end = local_day_bounds(today, tz)
        events = (
            await session.scalars(
                select(CalendarEvent).where(
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.jarvis_managed.is_(False),
                    CalendarEvent.start_at < day_end + timedelta(days=1),
                    CalendarEvent.end_at > day_start,
                )
            )
        ).all()
        # A task that is also a calendar event is already on the list (and reminded) as the event.
        soon = sorted(
            (
                t
                for t in tasks
                if t.due_datetime
                and now < t.due_datetime <= horizon
                and not any(same_item(t, e.summary, e.start_at, tz) for e in events)
            ),
            key=lambda t: t.due_datetime or now,
        )
        # Late in the day, date-only tasks for today are "close" too: the day is running out.
        late = to_local(now, tz).hour >= LATE_DAY_HOUR
        ending = sorted(
            (t for t in tasks if late and t.due_date == today and t.due_datetime is None),
            key=lambda t: (-t.priority, t.content),
        )
        items: list[Attention] = []
        for t in overdue:
            assert t.due_date is not None
            items.append(Attention(t, "overdue", f"просрочена с {t.due_date.strftime('%d.%m')}"))
        for t in missed:
            assert t.due_datetime is not None
            items.append(Attention(t, "missed", f"было на {format_local(t.due_datetime, tz)}"))
        for t in soon:
            assert t.due_datetime is not None
            minutes = int((t.due_datetime - now).total_seconds() // 60)
            items.append(Attention(t, "soon", f"в {format_local(t.due_datetime, tz)}, через {human_minutes(minutes)}"))
        for t in ending:
            items.append(Attention(t, "today", "сегодня, до конца дня"))
        return items[:limit]

    # --- leave time ---------------------------------------------------------------------------------------

    async def next_leave_text(self, session: AsyncSession, now: datetime) -> str | None:
        reminder = await session.scalar(
            select(Reminder)
            .where(
                Reminder.kind == "leave",
                Reminder.offset_minutes == 0,
                Reminder.fire_at >= now - timedelta(minutes=5),
                Reminder.status.in_(["scheduled", "firing", "sent"]),
            )
            .order_by(Reminder.fire_at)
            .limit(1)
        )
        if reminder is None:
            return None
        tz = self.settings.tz
        minutes = int((reminder.fire_at - now).total_seconds() // 60)
        what = reminder.payload.get("summary") or "встреча"
        if minutes <= 0:
            return f"🚶 Пора выходить на «{what}» (выход {format_local(reminder.fire_at, tz)})."
        return (
            f"🚶 Выходить на «{what}» в {format_local(reminder.fire_at, tz)} — через {human_minutes(minutes)} "
            f"(дорога ~{(reminder.payload.get('travel_seconds') or 0) // 60} мин + запас "
            f"{self.settings.reminder_travel_buffer_minutes} мин)."
        )  # plain text: the bot and the briefing escape it

    # --- briefings ----------------------------------------------------------------------------------------

    async def morning_brief(
        self, session: AsyncSession, now: datetime, *, calendar_connected: bool = True, address: str | None = None
    ) -> MorningBrief:
        tz = self.settings.tz
        today = to_local(now, tz).date()
        [view, tomorrow] = await self.days(session, Period(today, today + timedelta(days=1)), now)
        hello = f"Доброе утро, {address}!" if address else "Доброе утро!"
        greeting = fmt.line("☀️", fmt.b(hello), f"Сегодня {WEEKDAY_NAMES[today.weekday()]}, {today.strftime('%d.%m')}.")
        soon = (
            await session.scalars(
                select(TodoistTask)
                .where(
                    TodoistTask.checked.is_(False),
                    TodoistTask.is_deleted.is_(False),
                    TodoistTask.deadline_date > today,
                    TodoistTask.deadline_date <= today + timedelta(days=DEADLINE_LOOKAHEAD_DAYS),
                )
                .order_by(TodoistTask.deadline_date)
            )
        ).all()
        deadlines = fmt.section(
            "Скоро дедлайны",
            [fmt.line("•", fmt.b(t.deadline_date.strftime("%d.%m")), t.content) for t in soon if t.deadline_date],
            emoji="⏳",
        )
        tomorrow_line = fmt.line(
            fmt.head("Завтра:", "📆"),
            f"{plural(len(tomorrow.events), 'событие', 'события', 'событий')}, "
            f"{plural(len(tomorrow.tasks), 'задача', 'задачи', 'задач')}.",
        )
        return MorningBrief(
            greeting=greeting,
            day=self.day_lines(view) or fmt.hint("На сегодня ничего не запланировано."),
            deadlines=deadlines,
            leave=await self.next_leave_text(session, now),
            tomorrow=tomorrow_line,
            note=None if calendar_connected else fmt.hint("ℹ️ Google Calendar не подключён — показываю только задачи."),
        )

    async def evening_review(self, session: AsyncSession, now: datetime) -> EveningReview:
        tz = self.settings.tz
        today = to_local(now, tz).date()
        day_start, day_end = local_day_bounds(today, tz)
        undone = (
            await session.scalars(
                select(TodoistTask).where(
                    TodoistTask.checked.is_(False),
                    TodoistTask.is_deleted.is_(False),
                    TodoistTask.due_date <= today,
                    or_(TodoistTask.due_datetime.is_(None), TodoistTask.due_datetime <= now),
                )
            )
        ).all()
        done_today = (
            await session.scalars(
                select(TodoistTask.id).where(
                    TodoistTask.checked.is_(True), TodoistTask.synced_at >= day_start, TodoistTask.synced_at < day_end
                )
            )
        ).all()
        past_events = (
            await session.scalars(
                select(CalendarEvent).where(
                    CalendarEvent.status != "cancelled",
                    CalendarEvent.jarvis_managed.is_(False),
                    CalendarEvent.start_at >= day_start,
                    CalendarEvent.start_at < day_end,
                    CalendarEvent.end_at <= now,
                )
            )
        ).all()
        undone = sorted(undone, key=lambda t: (t.due_date or today, *_task_sort_key(t)))
        recurring = [t for t in undone if t.due_is_recurring]
        likely_done = [
            t for t in undone if not t.due_is_recurring and t.due_date == today and duplicates_event(t, past_events)
        ]
        to_move = [t for t in undone if not t.due_is_recurring and t not in likely_done]

        [tomorrow] = await self.days(session, Period(today + timedelta(days=1), today + timedelta(days=1)), now)
        summary: list[object] = [
            fmt.title("🌙", "Итоги дня", f"{WEEKDAY_NAMES[today.weekday()]} {today.strftime('%d.%m')}")
        ]
        if done_today:
            summary.append(fmt.line("✅", fmt.kv("Отмечено выполненным сегодня", len(done_today))))
        if not undone:
            summary.append("Все задачи на сегодня закрыты 👏")
        sections: list[object] = [summary]
        if to_move:
            sections.append(
                fmt.lines(
                    fmt.b(f"⏳ Не сделано ({len(to_move)}):"),
                    [self.task_line(t, show_date=t.due_date != today) for t in to_move],
                )
            )
        if likely_done:
            sections.append(
                fmt.lines(
                    fmt.b(f"📌 Были в календаре — возможно, уже сделано ({len(likely_done)}):"),
                    [self.task_line(t) for t in likely_done],
                )
            )
        if recurring:
            sections.append(
                fmt.line(
                    "🔁",
                    fmt.b("Повторяющиеся"),
                    fmt.i("(не переношу, только отметить):"),
                    ", ".join(t.content for t in recurring),
                )
            )
        first = next((e for e in tomorrow.events if not e.all_day), None)
        tomorrow_bits = [plural(len(tomorrow.events), "событие", "события", "событий")]
        tomorrow_bits.append(plural(len(tomorrow.tasks), "задача", "задачи", "задач"))
        first_text = (
            f", первое в {format_local(first.start_at, tz)} — {first.summary}" if first and first.start_at else ""
        )
        sections.append(fmt.line(fmt.head("Завтра:", "📆"), f"{', '.join(tomorrow_bits)}{first_text}."))
        hints: list[object] = []
        if to_move:
            hints.append(
                fmt.hint("➡️ Перенести — скажите: «перенеси всё невыполненное на завтра» или «стрижку на субботу».")
            )
        if likely_done:
            hints.append(fmt.hint("Кнопка ниже отметит их в Todoist. Не нажимать — всё останется как есть."))
        sections.append(hints)
        return EveningReview(fmt.blocks(*sections), to_move, likely_done)
