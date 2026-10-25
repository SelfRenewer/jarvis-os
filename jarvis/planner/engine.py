"""Deterministic slot planner.

Pure functions only — no I/O, no LLM. Given busy intervals, working hours and tasks, produce
placements that never overlap hard busy time or each other (including the configured break).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from jarvis.core.clock import ensure_aware, local_datetime, to_local


class Period(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


PERIOD_HOURS: dict[Period, tuple[time, time]] = {
    Period.MORNING: (time(6, 0), time(12, 0)),
    Period.AFTERNOON: (time(12, 0), time(17, 0)),
    Period.EVENING: (time(17, 0), time(23, 0)),
}


@dataclass(frozen=True, order=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.start)
        ensure_aware(self.end)
        if self.end < self.start:
            raise ValueError("interval end before start")

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: Interval) -> bool:
        return self.start <= other.start and other.end <= self.end


def subtract(windows: list[Interval], block: Interval) -> list[Interval]:
    result: list[Interval] = []
    for window in windows:
        if not window.overlaps(block):
            result.append(window)
            continue
        if window.start < block.start:
            result.append(Interval(window.start, block.start))
        if block.end < window.end:
            result.append(Interval(block.end, window.end))
    return result


@dataclass(frozen=True)
class BusyBlock:
    interval: Interval
    label: str = ""
    hard: bool = True


@dataclass(frozen=True)
class PlanningTask:
    task_id: str
    title: str
    duration_minutes: int
    priority: int = 1  # Todoist API: 4 = urgent … 1 = normal
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    hard_deadline: datetime | None = None
    preferred_periods: tuple[Period, ...] = ()
    location: str | None = None
    splittable: bool = False
    assumed_duration: bool = False


@dataclass(frozen=True)
class PlannerConfig:
    tz: ZoneInfo
    horizon_start: datetime
    horizon_end: datetime
    work_start: time = time(9, 0)
    work_end: time = time(21, 0)
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    sleep_start: time = time(23, 30)
    sleep_end: time = time(7, 30)
    min_break_minutes: int = 10
    granularity_minutes: int = 15
    min_chunk_minutes: int = 30


@dataclass(frozen=True)
class Placement:
    task_id: str
    interval: Interval
    score: float
    reasons: tuple[str, ...]


@dataclass
class PlanResult:
    placements: list[Placement] = field(default_factory=list)
    unschedulable: dict[str, str] = field(default_factory=dict)
    split_suggestions: dict[str, list[Interval]] = field(default_factory=dict)


def _ceil_to_grid(value: datetime, minutes: int) -> datetime:
    step = minutes * 60
    ts = int(value.timestamp())
    remainder = ts % step
    if remainder == 0 and value.microsecond == 0:
        return value
    return value + timedelta(seconds=step - remainder, microseconds=-value.microsecond)


def working_windows(cfg: PlannerConfig) -> list[Interval]:
    """Working hours minus sleep, per local day, clipped to the horizon."""
    windows: list[Interval] = []
    day = to_local(cfg.horizon_start, cfg.tz).date() - timedelta(days=1)
    last = to_local(cfg.horizon_end, cfg.tz).date()
    horizon = Interval(cfg.horizon_start, cfg.horizon_end)
    while day <= last:
        if day.weekday() in cfg.working_days:
            start = local_datetime(day, cfg.work_start, cfg.tz)
            end_day = day if cfg.work_end > cfg.work_start else day + timedelta(days=1)
            end = local_datetime(end_day, cfg.work_end, cfg.tz)
            day_windows = [Interval(start, end)]
            # remove sleep (which usually spans midnight): [sleep_start(day-1), sleep_end(day)] and today's
            for base in (day - timedelta(days=1), day):
                s_start = local_datetime(base, cfg.sleep_start, cfg.tz)
                s_end_day = base + timedelta(days=1) if cfg.sleep_end <= cfg.sleep_start else base
                s_end = local_datetime(s_end_day, cfg.sleep_end, cfg.tz)
                day_windows = subtract(day_windows, Interval(s_start, s_end))
            for window in day_windows:
                if window.overlaps(horizon):
                    windows.append(Interval(max(window.start, horizon.start), min(window.end, horizon.end)))
        day += timedelta(days=1)
    return sorted(w for w in windows if w.minutes > 0)


def free_windows(cfg: PlannerConfig, busy: list[BusyBlock]) -> list[Interval]:
    windows = working_windows(cfg)
    gap = timedelta(minutes=cfg.min_break_minutes)
    for block in busy:
        padded = Interval(block.interval.start - gap, block.interval.end + gap)
        windows = subtract(windows, padded)
    return [w for w in windows if w.minutes > 0]


def _period_of(start: datetime, end: datetime, tz: ZoneInfo) -> set[Period]:
    local_start = to_local(start, tz).time()
    local_end = to_local(end, tz).time()
    matched = set()
    for period, (p_start, p_end) in PERIOD_HOURS.items():
        if p_start <= local_start and (local_end <= p_end or local_end == time(0, 0)) and local_start < p_end:
            matched.add(period)
    return matched


def _score(task: PlanningTask, candidate: Interval, cfg: PlannerConfig) -> tuple[float, tuple[str, ...]]:
    reasons: list[str] = []
    hours_from_start = (candidate.start - cfg.horizon_start).total_seconds() / 3600
    urgency = 0.5 + 0.35 * max(1, min(task.priority, 4))
    score = -hours_from_start * urgency
    if task.preferred_periods:
        if _period_of(candidate.start, candidate.end, cfg.tz) & set(task.preferred_periods):
            score += 12
            reasons.append("preferred_period")
        else:
            score -= 4
    deadline = task.hard_deadline or task.latest_finish
    if deadline is not None:
        slack_hours = (deadline - candidate.end).total_seconds() / 3600
        if slack_hours < 1:
            score -= 2  # finishing right at the deadline is risky
        reasons.append(f"slack_{max(0, int(slack_hours))}h")
    return score, tuple(reasons)


def _sort_key(task: PlanningTask, far: datetime) -> tuple[datetime, int, int, str]:
    deadline = task.hard_deadline or task.latest_finish or far
    return (deadline, -task.priority, -task.duration_minutes, task.task_id)


def plan(tasks: list[PlanningTask], busy: list[BusyBlock], cfg: PlannerConfig) -> PlanResult:
    result = PlanResult()
    windows = free_windows(cfg, busy)
    step = timedelta(minutes=cfg.granularity_minutes)
    gap = timedelta(minutes=cfg.min_break_minutes)
    far = cfg.horizon_end + timedelta(days=3650)

    for task in sorted(tasks, key=lambda t: _sort_key(t, far)):
        if task.duration_minutes <= 0:
            result.unschedulable[task.task_id] = "invalid_duration"
            continue
        duration = timedelta(minutes=task.duration_minutes)
        latest = min(x for x in (task.latest_finish, task.hard_deadline, cfg.horizon_end) if x is not None)
        earliest = max(x for x in (task.earliest_start, cfg.horizon_start) if x is not None)
        best: tuple[float, Interval, tuple[str, ...]] | None = None
        for window in windows:
            start = _ceil_to_grid(max(window.start, earliest), cfg.granularity_minutes)
            while start + duration <= window.end and start + duration <= latest:
                candidate = Interval(start, start + duration)
                score, reasons = _score(task, candidate, cfg)
                if best is None or score > best[0]:
                    best = (score, candidate, reasons)
                start += step
        if best is None:
            longest = max((w.minutes for w in windows), default=0)
            if task.splittable and longest >= cfg.min_chunk_minutes:
                result.split_suggestions[task.task_id] = _suggest_chunks(task, windows, cfg, earliest, latest)
                result.unschedulable[task.task_id] = "needs_split"
            elif longest < task.duration_minutes:
                result.unschedulable[task.task_id] = "no_slot_long_enough"
            else:
                result.unschedulable[task.task_id] = "no_slot_before_deadline"
            continue
        score, interval, reasons = best
        result.placements.append(Placement(task.task_id, interval, round(score, 3), reasons))
        windows = subtract(windows, Interval(interval.start - gap, interval.end + gap))
    result.placements.sort(key=lambda p: p.interval.start)
    return result


def _suggest_chunks(
    task: PlanningTask, windows: list[Interval], cfg: PlannerConfig, earliest: datetime, latest: datetime
) -> list[Interval]:
    remaining = timedelta(minutes=task.duration_minutes)
    minimum = timedelta(minutes=cfg.min_chunk_minutes)
    chunks: list[Interval] = []
    for window in windows:
        start = _ceil_to_grid(max(window.start, earliest), cfg.granularity_minutes)
        end = min(window.end, latest)
        if end - start < minimum:
            continue
        take = min(remaining, end - start)
        chunks.append(Interval(start, start + take))
        remaining -= take
        if remaining <= timedelta(0):
            break
    return chunks if remaining <= timedelta(0) else []
