"""Per-day usage budgets stored in PostgreSQL (atomic upsert-increment)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.errors import BudgetExceededError
from jarvis.db.models import UsageCounter


async def consume(session: AsyncSession, provider: str, now: datetime, limit: int, amount: int = 1) -> int:
    """Atomically add `amount` to today's counter; raise if the new value would exceed `limit`.

    The increment is committed together with the caller's transaction. A rejected consume does not
    increment (the conditional `WHERE` makes the upsert a no-op).
    """
    day = now.date()
    stmt = (
        insert(UsageCounter)
        .values(provider=provider, day=day, count=amount)
        .on_conflict_do_update(
            index_elements=[UsageCounter.provider, UsageCounter.day],
            set_={"count": UsageCounter.count + amount},
            where=UsageCounter.count + amount <= limit,
        )
        .returning(UsageCounter.count)
    )
    if amount > limit:
        raise BudgetExceededError(f"{provider}: daily budget {limit} exhausted")
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise BudgetExceededError(f"{provider}: daily budget {limit} exhausted")
    return int(row[0])


async def used_today(session: AsyncSession, provider: str, now: datetime) -> int:
    value = await session.scalar(
        select(UsageCounter.count).where(UsageCounter.provider == provider, UsageCounter.day == now.date())
    )
    return int(value or 0)
