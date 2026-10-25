"""Materializes periodic ("cron-like") work as ordinary durable jobs.

Each tick computes the current slot for every periodic task and enqueues a job whose dedupe key is
derived from the slot. Running several schedulers (or restarting one) cannot double-enqueue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jarvis.core.clock import Clock, local_datetime
from jarvis.db.session import Database
from jarvis.scheduler.queue import enqueue

log = logging.getLogger(__name__)

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class PeriodicTask:
    name: str
    job_type: str
    every: timedelta | None = None  # fixed interval
    daily_at: time | None = None  # local wall-clock time
    payload: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 3
    priority: int = 150

    def slot(self, now: datetime, tz: ZoneInfo) -> tuple[str, datetime] | None:
        """Return (slot_id, run_at) of the slot that is due at `now`, if any."""
        if self.every is not None:
            seconds = int(self.every.total_seconds())
            index = int((now - EPOCH).total_seconds()) // seconds
            start = EPOCH + timedelta(seconds=index * seconds)
            return start.isoformat(), start
        if self.daily_at is not None:
            local_today: date = now.astimezone(tz).date()
            run_at = local_datetime(local_today, self.daily_at, tz)
            if now < run_at:
                return None
            return local_today.isoformat(), run_at
        raise ValueError("periodic task needs `every` or `daily_at`")


class PeriodicScheduler:
    def __init__(self, db: Database, clock: Clock, tz: ZoneInfo, tasks: list[PeriodicTask]) -> None:
        self.db = db
        self.clock = clock
        self.tz = tz
        self.tasks = tasks

    async def tick(self) -> list[str]:
        now = self.clock.now()
        created: list[str] = []
        async with self.db.session() as session:
            for task in self.tasks:
                slot = task.slot(now, self.tz)
                if slot is None:
                    continue
                slot_id, run_at = slot
                job_id = await enqueue(
                    session,
                    job_type=task.job_type,
                    run_at=max(run_at, now - timedelta(seconds=1)),
                    payload={**task.payload, "periodic": task.name, "slot": slot_id},
                    dedupe_key=f"periodic:{task.name}:{slot_id}",
                    max_attempts=task.max_attempts,
                    priority=task.priority,
                )
                if job_id is not None:
                    created.append(task.name)
        return created

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        interval: float = 15.0,
        extra: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        while not stop.is_set():
            try:
                created = await self.tick()
                if created:
                    log.info("periodic jobs enqueued: %s", ", ".join(created))
                if extra is not None:
                    await extra()
            except Exception:
                log.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
