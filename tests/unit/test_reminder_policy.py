from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.reminders.policy import (
    Channel,
    Kind,
    LateAction,
    ReminderPolicy,
    compute_leave_at,
    decide_late_action,
    plan_reminders,
)

TZ = ZoneInfo("Asia/Tbilisi")


def at(h: int, m: int) -> datetime:
    return datetime(2026, 9, 23, h, m, tzinfo=TZ)


def test_scenario2_leave_time() -> None:
    """15:00 appointment, 22 min travel, 10 min buffer → leave at 14:28."""
    leave = compute_leave_at(at(15, 0), 22 * 60, 10)
    assert leave == at(14, 28)
    assert leave.astimezone(UTC) == datetime(2026, 9, 23, 10, 28, tzinfo=UTC)


def test_travel_rounds_up_to_minute() -> None:
    assert compute_leave_at(at(15, 0), 22 * 60 + 1, 10) == at(14, 27)


def test_scenario2_reminder_times() -> None:
    policy = ReminderPolicy(leave_offsets_minutes=(30, 8, 0), call_offsets_minutes=(0,), calls_enabled=True)
    items = plan_reminders(event_start=at(15, 0), leave_at=at(14, 28), has_location=True, policy=policy)
    assert [(i.fire_at, i.channel) for i in items] == [
        (at(13, 58), Channel.TELEGRAM),
        (at(14, 20), Channel.TELEGRAM),
        (at(14, 28), Channel.CALL),
    ]
    assert all(i.kind == Kind.LEAVE for i in items)


def test_calls_disabled_means_telegram_only() -> None:
    items = plan_reminders(
        event_start=at(15, 0), leave_at=at(14, 28), has_location=True, policy=ReminderPolicy(calls_enabled=False)
    )
    assert {i.channel for i in items} == {Channel.TELEGRAM}


def test_no_route_never_invents_travel() -> None:
    items = plan_reminders(event_start=at(15, 0), leave_at=None, has_location=True, policy=ReminderPolicy())
    assert [i.fire_at for i in items] == [at(14, 0), at(14, 30)]
    assert all(i.kind == Kind.START and i.anchor == at(15, 0) for i in items)


def test_no_location_start_reminder() -> None:
    items = plan_reminders(event_start=at(15, 0), leave_at=None, has_location=False, policy=ReminderPolicy())
    assert [i.fire_at for i in items] == [at(14, 45)]


@pytest.mark.parametrize(
    ("now", "channel", "expected"),
    [
        (at(14, 30), Channel.CALL, LateAction.DELIVER),  # 2 min late
        (at(14, 34), Channel.CALL, LateAction.RECOVERY_NOTICE),  # 6 min > 5 min grace
        (at(14, 50), Channel.TELEGRAM, LateAction.DELIVER),  # 22 min < 30 min grace
        (at(16, 30), Channel.CALL, LateAction.MISSED),  # hours late: never call
    ],
)
def test_grace_policy(now: datetime, channel: Channel, expected: LateAction) -> None:
    assert (
        decide_late_action(
            now=now,
            fire_at=at(14, 28),
            event_start=at(15, 0),
            channel=channel,
            call_grace=timedelta(minutes=5),
            telegram_grace=timedelta(minutes=30),
        )
        == expected
    )
