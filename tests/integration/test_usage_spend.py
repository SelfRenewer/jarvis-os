"""Spend per period from hourly samples of the OpenRouter key (no history API for a normal key)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jarvis.db.models import AgentRun, SpendSnapshot
from jarvis.usage.service import PERIODS, Spend, UsageService, calendar_points, to_micro, usd

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


async def seed(c: Any) -> None:
    async with c.db.session() as s:
        for day in (1, 5, 15):
            s.add(AgentRun(capability="intent_router", model="m", status="ok", created_at=at(day, 12)))
        s.add(AgentRun(capability="intent_router", model="m", status="ok", created_at=at(22, 3)))
        s.add(SpendSnapshot(taken_at=at(10), usage_micro=100_000, source="key"))
        s.add(SpendSnapshot(taken_at=at(20), usage_micro=150_000, source="key"))


async def test_spend_per_period(container: Any) -> None:
    c = container
    await seed(c)
    service = UsageService()
    total = 220_000
    async with c.db.session() as s:
        # 30 days back is before Jarvis's first paid call (01.09): everything spent since then
        assert await service.spend_since(s, NOW - timedelta(days=30), total) == Spend(220_000)
        # 7 days back (16.09): the sample of 10.09 is not usable (a call on 15.09), the one of 20.09 is (no calls)
        assert await service.spend_since(s, NOW - timedelta(days=7), total) == Spend(70_000)
        # 24 h back (22.09 05:00): a call at 03:00 after the 20.09 sample and no sample after → an upper bound
        assert await service.spend_since(s, NOW - timedelta(days=1), total) == Spend(70_000, exact=False)
        assert await service.spend_since(s, None, total) == Spend(220_000)


async def test_record_derives_calendar_points(container: Any) -> None:
    c = container
    key = {"usage": "0.30", "usage_daily": "0.05", "usage_weekly": "0.10", "usage_monthly": "0.25"}
    points = calendar_points(key, NOW)  # Wed 23.09: day 23.09, week from Mon 21.09, month from 01.09 (UTC)
    assert points == [
        (NOW, 300_000, "key"),
        (at(23), 250_000, "day"),
        (at(21), 200_000, "week"),
        (at(1), 50_000, "month"),
    ]
    service = UsageService()
    async with c.db.session() as s:
        assert await service.record(s, key, NOW) == 300_000
        assert await service.record(s, key, NOW) == 300_000  # the same instants are kept, not duplicated
        assert await service.tracked_since(s) == NOW


def test_money_formatting_and_periods() -> None:
    assert to_micro("0.222109254") == 222_109 and to_micro(None) == 0 and to_micro("bad") == 0
    assert usd(222_109) == "$0.22" and usd(72_000) == "$0.072" and usd(0) == "$0.00"
    assert Spend(70_000, exact=False).text() == "≤$0.070"
    assert PERIODS["m3"].since(NOW) == datetime(2026, 6, 23, 5, 0, tzinfo=UTC)
    assert PERIODS["m6"].since(NOW) == datetime(2026, 3, 23, 5, 0, tzinfo=UTC)
    assert PERIODS["all"].since(NOW) is None
