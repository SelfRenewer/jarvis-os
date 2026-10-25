"""Time source and timezone helpers.

Rules enforced here:
* every datetime crossing a module boundary is timezone-aware;
* internal storage/computation uses UTC;
* user-facing conversion uses IANA zone names (DST-safe), never fixed offsets.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class NaiveDatetimeError(ValueError):
    pass


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(f"naive datetime is not allowed: {value!r}")
    return value


def to_utc(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(UTC)


def to_local(value: datetime, tz: ZoneInfo) -> datetime:
    return ensure_aware(value).astimezone(tz)


def local_datetime(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Wall-clock `day at` in `tz` → aware datetime.

    Non-existent local times (DST gap) are shifted forward by the gap, ambiguous ones (DST overlap)
    resolve to the first occurrence (fold=0) — the standard `zoneinfo` behaviour made explicit.
    """
    naive = datetime.combine(day, at)
    candidate = naive.replace(tzinfo=tz)
    # Round-trip through UTC detects a non-existent wall time.
    round_trip = candidate.astimezone(UTC).astimezone(tz)
    if round_trip.replace(tzinfo=None) != naive:
        return round_trip
    return candidate


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of a local calendar day, as UTC datetimes (handles 23h/25h DST days)."""
    start = local_datetime(day, time(0, 0), tz)
    end = local_datetime(day + timedelta(days=1), time(0, 0), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp; values without an offset are rejected."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return ensure_aware(parsed)


def format_local(value: datetime, tz: ZoneInfo, fmt: str = "%H:%M") -> str:
    return to_local(value, tz).strftime(fmt)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock for tests and simulations."""

    def __init__(self, start: datetime) -> None:
        self._now = to_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = to_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now
