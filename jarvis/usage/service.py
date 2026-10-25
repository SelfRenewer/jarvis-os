"""Usage statistics periods and OpenRouter spend per period.

OpenRouter gives a normal key only its cumulative `usage` plus calendar counters (UTC day / week / month); the
history endpoint needs a management key. So Jarvis samples the cumulative value hourly (`usage.snapshot`, a free
call) and computes the spend over a period as `usage now − usage at the period start`.

Exactness: the key is used only by Jarvis, so the cumulative value does not change while Jarvis makes no paid
calls (LLM runs, voice synthesis, transcription). A sample before the period start is therefore exact when there
was no paid activity between it and the start; the same for a sample after the start. Before Jarvis's first paid
call the value is 0. Otherwise the result is an upper bound and is shown with «≤».

Money is integer micro-dollars (1 USD = 1 000 000), converted from the API's decimal strings via Decimal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.dates import add_months
from jarvis.db.models import AgentRun, SpendSnapshot, UsageCounter

MICRO = Decimal(1_000_000)
PAID_COUNTERS = ("tts", "stt")  # paid OpenRouter calls counted per day besides the LLM runs


@dataclass(frozen=True)
class Period:
    key: str
    label: str  # column header
    title: str  # «за 30 дней»
    days: int = 0
    months: int = 0

    def since(self, now: datetime) -> datetime | None:
        """Start of the period (None = all time)."""
        if self.months:
            local = now.astimezone(UTC)
            return datetime.combine(add_months(local.date(), -self.months), local.timetz())
        if self.days:
            return now - timedelta(days=self.days)
        return None


PERIODS: dict[str, Period] = {
    p.key: p
    for p in (
        Period("d1", "24 ч", "за 24 часа", days=1),
        Period("d7", "7 дн", "за 7 дней", days=7),
        Period("d30", "30 дн", "за 30 дней", days=30),
        Period("m3", "3 мес", "за 3 месяца", months=3),
        Period("m6", "6 мес", "за 6 месяцев", months=6),
        Period("all", "всё время", "за всё время"),
    )
}
DEFAULT_PERIOD = "d30"


@dataclass(frozen=True)
class Spend:
    micro: int
    exact: bool = True

    def text(self) -> str:
        return ("" if self.exact else "≤") + usd(self.micro)


def to_micro(value: Any) -> int:
    """API dollars (float/str) → integer micro-dollars, through Decimal (never float arithmetic)."""
    try:
        return int((Decimal(str(value or 0)) * MICRO).to_integral_value())
    except (InvalidOperation, ValueError):
        return 0


def usd(micro: int) -> str:
    """$0.22 · small amounts keep a third digit ($0.072) so a few cents of spend never shows as $0.00."""
    amount = Decimal(micro) / MICRO
    places = Decimal("0.001") if 0 < abs(amount) < Decimal("0.1") else Decimal("0.01")
    return f"${amount.quantize(places)}"


def calendar_points(key: Mapping[str, Any], now: datetime) -> list[tuple[datetime, int, str]]:
    """Cumulative usage at known instants derived from one `/key` answer: now, and the starts of the current UTC
    day, ISO week (Monday) and month (usage − usage_daily … − usage_monthly)."""
    total = to_micro(key.get("usage"))
    utc = now.astimezone(UTC)
    day = datetime.combine(utc.date(), time(0), tzinfo=UTC)
    points = [(now, total, "key")]
    for start, field, source in (
        (day, "usage_daily", "day"),
        (day - timedelta(days=utc.weekday()), "usage_weekly", "week"),
        (day.replace(day=1), "usage_monthly", "month"),
    ):
        if key.get(field) is not None:
            points.append((start, max(0, total - to_micro(key.get(field))), source))
    return points


class UsageService:
    async def record(self, session: AsyncSession, key: Mapping[str, Any], now: datetime) -> int:
        """Store a sample (and the derived calendar points; existing ones are kept). Returns the total."""
        for taken_at, micro, source in calendar_points(key, now):
            await session.execute(
                insert(SpendSnapshot)
                .values(taken_at=taken_at, usage_micro=micro, source=source)
                .on_conflict_do_nothing(index_elements=[SpendSnapshot.taken_at])
            )
        return to_micro(key.get("usage"))

    async def _paid_activity(self, session: AsyncSession, start: datetime, end: datetime) -> bool:
        """Any paid call in (start, end]: an LLM run, or a voice synthesis/transcription that day."""
        if end <= start:
            return False
        runs = await session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.created_at > start, AgentRun.created_at <= end)
        )
        if runs:
            return True
        counters = await session.scalar(
            select(func.coalesce(func.sum(UsageCounter.count), 0)).where(
                UsageCounter.provider.in_(PAID_COUNTERS),
                UsageCounter.day >= start.astimezone(UTC).date() - timedelta(days=1),
                UsageCounter.day <= end.astimezone(UTC).date() + timedelta(days=1),
            )
        )
        return bool(counters)

    async def _first_paid(self, session: AsyncSession) -> datetime | None:
        first_run = await session.scalar(select(func.min(AgentRun.created_at)))
        first_day = await session.scalar(
            select(func.min(UsageCounter.day)).where(UsageCounter.provider.in_(PAID_COUNTERS), UsageCounter.count > 0)
        )
        candidates = [first_run] if first_run is not None else []
        if first_day is not None:
            candidates.append(datetime.combine(first_day, time(0), tzinfo=UTC) - timedelta(days=1))
        return min(candidates) if candidates else None

    async def spend_since(self, session: AsyncSession, since: datetime | None, total: int) -> Spend:
        """Spend of the key from `since` (None = all time) to now, given the current cumulative `total`."""
        if since is None:
            return Spend(total)
        first = await self._first_paid(session)
        if first is None or since < first:
            return Spend(total)  # nothing was spent before Jarvis's first paid call
        before = (
            await session.execute(
                select(SpendSnapshot.taken_at, SpendSnapshot.usage_micro)
                .where(SpendSnapshot.taken_at <= since)
                .order_by(SpendSnapshot.taken_at.desc())
                .limit(1)
            )
        ).first()
        if before is not None and not await self._paid_activity(session, before[0], since):
            return Spend(max(0, total - int(before[1])))
        after = (
            await session.execute(
                select(SpendSnapshot.taken_at, SpendSnapshot.usage_micro)
                .where(SpendSnapshot.taken_at >= since)
                .order_by(SpendSnapshot.taken_at)
                .limit(1)
            )
        ).first()
        if after is not None and not await self._paid_activity(session, since, after[0]):
            return Spend(max(0, total - int(after[1])))
        floor = int(before[1]) if before is not None else 0
        return Spend(max(0, total - floor), exact=False)

    async def tracked_since(self, session: AsyncSession) -> datetime | None:
        return await session.scalar(select(func.min(SpendSnapshot.taken_at)).where(SpendSnapshot.source == "key"))
