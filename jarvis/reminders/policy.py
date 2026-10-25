"""Pure reminder timing rules (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from jarvis.core.clock import ensure_aware


class Channel(StrEnum):
    TELEGRAM = "telegram"
    CALL = "call"


class Kind(StrEnum):
    LEAVE = "leave"  # anchored to the departure time (travel time known)
    START = "start"  # anchored to the event start (no travel needed / unknown)


@dataclass(frozen=True)
class ReminderPolicy:
    leave_offsets_minutes: tuple[int, ...] = (30, 8, 0)
    call_offsets_minutes: tuple[int, ...] = (0,)
    start_offsets_minutes: tuple[int, ...] = (15,)
    no_route_offsets_minutes: tuple[int, ...] = (60, 30)
    travel_buffer_minutes: int = 10
    calls_enabled: bool = False


@dataclass(frozen=True)
class PlannedReminder:
    kind: Kind
    offset_minutes: int
    channel: Channel
    fire_at: datetime
    anchor: datetime  # leave_at for LEAVE, event start for START


def compute_leave_at(event_start: datetime, travel_seconds: int, buffer_minutes: int) -> datetime:
    """leave_at = event_start − travel − buffer (travel rounded up to whole minutes)."""
    ensure_aware(event_start)
    if travel_seconds < 0 or buffer_minutes < 0:
        raise ValueError("travel and buffer must be non-negative")
    travel_minutes = -(-travel_seconds // 60)
    return event_start - timedelta(minutes=travel_minutes + buffer_minutes)


def plan_reminders(
    *,
    event_start: datetime,
    leave_at: datetime | None,
    has_location: bool,
    policy: ReminderPolicy,
) -> list[PlannedReminder]:
    ensure_aware(event_start)
    items: list[PlannedReminder] = []
    if leave_at is not None:
        for offset in sorted(set(policy.leave_offsets_minutes), reverse=True):
            call = policy.calls_enabled and offset in policy.call_offsets_minutes
            items.append(
                PlannedReminder(
                    Kind.LEAVE,
                    offset,
                    Channel.CALL if call else Channel.TELEGRAM,
                    leave_at - timedelta(minutes=offset),
                    leave_at,
                )
            )
        return items
    offsets = policy.no_route_offsets_minutes if has_location else policy.start_offsets_minutes
    for offset in sorted(set(offsets), reverse=True):
        items.append(
            PlannedReminder(Kind.START, offset, Channel.TELEGRAM, event_start - timedelta(minutes=offset), event_start)
        )
    return items


class LateAction(StrEnum):
    DELIVER = "deliver"
    RECOVERY_NOTICE = "recovery_notice"  # too late for the planned channel, still useful to know
    MISSED = "missed"  # the event already started


def decide_late_action(
    *,
    now: datetime,
    fire_at: datetime,
    event_start: datetime,
    channel: Channel,
    call_grace: timedelta,
    telegram_grace: timedelta,
) -> LateAction:
    if now >= event_start:
        return LateAction.MISSED
    lateness = now - fire_at
    grace = call_grace if channel == Channel.CALL else telegram_grace
    if lateness <= grace:
        return LateAction.DELIVER
    return LateAction.RECOVERY_NOTICE


def minutes_between(later: datetime, earlier: datetime) -> int:
    return max(0, int((later - earlier).total_seconds() // 60))
