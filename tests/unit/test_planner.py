from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from jarvis.planner.constraints import parse_preferences_keywords
from jarvis.planner.engine import (
    BusyBlock,
    Interval,
    Period,
    PlannerConfig,
    PlanningTask,
    free_windows,
    plan,
    working_windows,
)

TZ = ZoneInfo("Asia/Tbilisi")


def local(h: int, m: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ).astimezone(UTC)


def cfg(**kw: object) -> PlannerConfig:
    base: dict[str, object] = {
        "tz": TZ,
        "horizon_start": local(9, 0),
        "horizon_end": local(23, 59),
        "work_start": time(9, 0),
        "work_end": time(21, 0),
        "min_break_minutes": 10,
    }
    base.update(kw)
    return PlannerConfig(**base)  # type: ignore[arg-type]


def test_working_windows_respect_hours() -> None:
    windows = working_windows(cfg())
    assert windows == [Interval(local(9), local(21))]


def test_sleep_window_is_removed() -> None:
    windows = working_windows(cfg(work_start=time(6, 0), work_end=time(23, 59), horizon_start=local(6)))
    assert windows[0].start == local(7, 30)
    assert windows[-1].end == local(23, 30)


def test_barbershop_scenario_avoids_conflicts() -> None:
    """Scenario 1: 60-min task due today, calendar has conflicting appointments."""
    busy = [
        BusyBlock(Interval(local(9, 0), local(12, 30)), "work block"),
        BusyBlock(Interval(local(13, 0), local(14, 0)), "lunch meeting"),
        BusyBlock(Interval(local(14, 30), local(18, 0)), "client"),
    ]
    task = PlanningTask("t1", "Сходить в барбершоп", 60, priority=3, latest_finish=local(21, 0))
    result = plan([task], busy, cfg())
    assert result.unschedulable == {}
    placement = result.placements[0]
    assert placement.interval.minutes == 60
    for block in busy:
        padded = Interval(block.interval.start - timedelta(minutes=10), block.interval.end + timedelta(minutes=10))
        assert not placement.interval.overlaps(padded)
    assert placement.interval.start == local(18, 15)  # first legal slot after the break


def test_preferred_evening() -> None:
    task = PlanningTask("t1", "Спорт", 60, preferred_periods=(Period.EVENING,))
    result = plan([task], [], cfg())
    assert result.placements[0].interval.start >= local(17)
    assert "preferred_period" in result.placements[0].reasons


def test_deadline_respected_and_priority_order() -> None:
    tasks = [
        PlanningTask("low", "low", 120, priority=1),
        PlanningTask("urgent", "urgent", 60, priority=4, hard_deadline=local(11, 0)),
    ]
    result = plan(tasks, [], cfg())
    by_id = {p.task_id: p for p in result.placements}
    assert by_id["urgent"].interval.end <= local(11, 0)
    assert not by_id["urgent"].interval.overlaps(by_id["low"].interval)


def test_unschedulable_when_too_long() -> None:
    busy = [BusyBlock(Interval(local(10), local(20)), "all day")]
    result = plan([PlanningTask("t", "long", 180)], busy, cfg())
    assert result.unschedulable == {"t": "no_slot_long_enough"}


def test_splittable_task_gets_chunk_suggestions() -> None:
    busy = [
        BusyBlock(Interval(local(11), local(12)), "a"),
        BusyBlock(Interval(local(14), local(15)), "b"),
        BusyBlock(Interval(local(17), local(21)), "c"),
    ]
    task = PlanningTask("t", "essay", 300, splittable=True)
    result = plan([task], busy, cfg(horizon_end=local(21)))
    assert result.unschedulable["t"] == "needs_split"
    chunks = result.split_suggestions["t"]
    assert sum(c.minutes for c in chunks) == 300


def test_keyword_preferences() -> None:
    assert parse_preferences_keywords("лучше вечером") == (Period.EVENING,)
    assert parse_preferences_keywords("утром или днём") == (Period.MORNING, Period.AFTERNOON)
    assert parse_preferences_keywords("без разницы") == ()


# --- property: no overlaps ever -------------------------------------------------------------------

busy_strategy = st.lists(
    st.tuples(st.integers(min_value=0, max_value=14 * 4 - 1), st.integers(min_value=1, max_value=12)),
    max_size=8,
)
task_strategy = st.lists(
    st.tuples(st.integers(min_value=1, max_value=16), st.integers(min_value=1, max_value=4)),
    min_size=1,
    max_size=8,
)


@settings(max_examples=150, deadline=None)
@given(busy_strategy, task_strategy)
def test_property_placements_never_overlap(busy_spec: list[tuple[int, int]], task_spec: list[tuple[int, int]]) -> None:
    config = cfg()
    busy = [
        BusyBlock(Interval(local(9) + timedelta(minutes=15 * s), local(9) + timedelta(minutes=15 * (s + d))))
        for s, d in busy_spec
    ]
    tasks = [PlanningTask(f"t{i}", "x", 15 * q, priority=p) for i, (q, p) in enumerate(task_spec)]
    result = plan(tasks, busy, config)
    windows = working_windows(config)
    placed = [p.interval for p in result.placements]
    for i, a in enumerate(placed):
        assert any(w.contains(a) for w in windows)
        for block in busy:
            assert not a.overlaps(block.interval)
        for b in placed[i + 1 :]:
            assert not a.overlaps(b)
    assert len(result.placements) + len(result.unschedulable) == len(tasks)
    # every placed interval also lies in the originally free windows
    free = free_windows(config, busy)
    for a in placed:
        assert any(w.contains(a) for w in free)
