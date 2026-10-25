"""Durable scheduler: dedupe, SKIP LOCKED concurrency, retries, dead-letter, crash recovery."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from jarvis.core.clock import FrozenClock
from jarvis.core.errors import CircuitOpenError, PermanentError, TransientError
from jarvis.core.retry import RetryPolicy
from jarvis.db.models import JobAttempt, ScheduledJob
from jarvis.db.session import Database
from jarvis.scheduler.periodic import PeriodicScheduler, PeriodicTask
from jarvis.scheduler.queue import JobQueue, enqueue
from jarvis.scheduler.runner import HandlerRegistry, JobContext, SkipJob, Worker

pytestmark = pytest.mark.db

NO_JITTER = RetryPolicy(base_seconds=10, cap_seconds=60, max_attempts=3, jitter_ratio=0)


class _Container:
    def __init__(self, db: Database) -> None:
        self.db = db


async def test_enqueue_dedupe(db: Database, clock: FrozenClock) -> None:
    async with db.session() as s:
        first = await enqueue(s, job_type="x", run_at=clock.now(), dedupe_key="k1")
        second = await enqueue(s, job_type="x", run_at=clock.now(), dedupe_key="k1")
    assert first is not None
    assert second is None
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(ScheduledJob)) == 1


async def test_concurrent_claims_never_overlap(db: Database, clock: FrozenClock) -> None:
    async with db.session() as s:
        for i in range(40):
            await enqueue(s, job_type="x", run_at=clock.now(), dedupe_key=f"c{i}")
    queues = [JobQueue(db, clock, worker_id=f"w{i}") for i in range(5)]
    results = await asyncio.gather(*(q.claim(limit=10) for q in queues))
    ids = [job.id for batch in results for job in batch]
    assert len(ids) == 40
    assert len(set(ids)) == 40


async def test_future_jobs_not_claimed(db: Database, clock: FrozenClock) -> None:
    async with db.session() as s:
        await enqueue(s, job_type="x", run_at=clock.now() + timedelta(minutes=5))
    queue = JobQueue(db, clock, worker_id="w")
    assert await queue.claim() == []
    clock.advance(timedelta(minutes=5))
    assert len(await queue.claim()) == 1


async def test_retry_backoff_then_dead(db: Database, clock: FrozenClock) -> None:
    calls: list[int] = []

    async def flaky(ctx: JobContext) -> dict[str, Any]:
        calls.append(ctx.job.attempts)
        raise TransientError("temporary")

    registry = HandlerRegistry()
    registry.register("flaky", flaky)
    queue = JobQueue(db, clock, worker_id="w", retry=NO_JITTER)
    worker = Worker(queue, registry, _Container(db))  # type: ignore[arg-type]
    async with db.session() as s:
        job_id = await enqueue(s, job_type="flaky", run_at=clock.now(), max_attempts=3)

    assert await worker.run_once() == 1
    async with db.session() as s:
        job = await s.get(ScheduledJob, job_id)
        assert job is not None and job.status == "pending"
        assert job.run_at == clock.now() + timedelta(seconds=10)
    assert await worker.run_once() == 0  # not due yet
    clock.advance(timedelta(seconds=10))
    await worker.run_once()
    clock.advance(timedelta(seconds=20))
    await worker.run_once()
    async with db.session() as s:
        job = await s.get(ScheduledJob, job_id)
        assert job is not None and job.status == "dead"
        assert "temporary" in (job.last_error or "")
        attempts = (await s.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).all()
    assert calls == [1, 2, 3]
    assert [a.status for a in sorted(attempts, key=lambda a: a.attempt_no)] == ["retry", "retry", "dead"]


async def test_permanent_error_goes_dead_immediately(db: Database, clock: FrozenClock) -> None:
    async def bad(ctx: JobContext) -> None:
        raise PermanentError("invalid")

    registry = HandlerRegistry()
    registry.register("bad", bad)
    worker = Worker(JobQueue(db, clock, worker_id="w"), registry, _Container(db))  # type: ignore[arg-type]
    async with db.session() as s:
        job_id = await enqueue(s, job_type="bad", run_at=clock.now())
    await worker.run_once()
    async with db.session() as s:
        job = await s.get(ScheduledJob, job_id)
        assert job is not None and job.status == "dead" and job.attempts == 1


async def test_skip_job(db: Database, clock: FrozenClock) -> None:
    async def skipper(ctx: JobContext) -> None:
        raise SkipJob("event cancelled")

    registry = HandlerRegistry()
    registry.register("s", skipper)
    worker = Worker(JobQueue(db, clock, worker_id="w"), registry, _Container(db))  # type: ignore[arg-type]
    async with db.session() as s:
        job_id = await enqueue(s, job_type="s", run_at=clock.now())
    await worker.run_once()
    async with db.session() as s:
        job = await s.get(ScheduledJob, job_id)
        assert job is not None and job.status == "skipped" and job.result == {"reason": "event cancelled"}


async def test_crash_recovery_via_lease_expiry(db: Database, clock: FrozenClock) -> None:
    executed: list[str] = []

    async def ok(ctx: JobContext) -> dict[str, Any]:
        executed.append(str(ctx.job.id))
        return {"done": True}

    async with db.session() as s:
        job_id = await enqueue(s, job_type="ok", run_at=clock.now())
    crashed = JobQueue(db, clock, worker_id="crashed", lease_seconds=60)
    claimed = await crashed.claim()
    assert len(claimed) == 1  # worker "crashes" here without finishing

    registry = HandlerRegistry()
    registry.register("ok", ok)
    survivor_queue = JobQueue(db, clock, worker_id="survivor", lease_seconds=60)
    survivor = Worker(survivor_queue, registry, _Container(db))  # type: ignore[arg-type]
    assert await survivor.run_once() == 0  # still leased
    clock.advance(timedelta(seconds=61))
    assert await survivor_queue.reap_expired_leases() == {"requeued": 1, "dead": 0}
    assert await survivor.run_once() == 1
    # the crashed worker cannot complete a job it no longer owns
    assert await crashed.complete(claimed[0], {"late": True}) is False
    async with db.session() as s:
        job = await s.get(ScheduledJob, job_id)
        assert job is not None and job.status == "succeeded" and job.result == {"done": True}
    assert executed == [str(job_id)]


async def test_periodic_scheduler_dedupes_across_instances(db: Database, clock: FrozenClock, settings: Any) -> None:
    tasks = [
        PeriodicTask(name="recon", job_type="todoist.reconcile", every=timedelta(minutes=10)),
        PeriodicTask(name="daily", job_type="planner.daily", daily_at=__import__("datetime").time(9, 0)),
    ]
    a = PeriodicScheduler(db, clock, settings.tz, tasks)
    b = PeriodicScheduler(db, clock, settings.tz, tasks)
    assert sorted(await a.tick()) == ["daily", "recon"]  # 09:00 Tbilisi == now
    assert await b.tick() == []
    clock.advance(timedelta(minutes=10))
    assert await b.tick() == ["recon"]
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(ScheduledJob)) == 3


async def test_a_hung_handler_is_cut_off_and_retried(db: Database, clock: FrozenClock) -> None:
    # M1 (review 17.10): a handler that never returned held its batch, the reaper and every later job.
    registry = HandlerRegistry()

    async def hang(ctx: JobContext) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    registry.register("hang", hang)
    queue = JobQueue(db, clock, worker_id="w", retry=NO_JITTER)
    worker = Worker(queue, registry, _Container(db), timeouts={"hang": 0.2})  # type: ignore[arg-type]
    async with db.session() as s:
        await enqueue(s, job_type="hang", run_at=clock.now())
    assert await asyncio.wait_for(worker.run_once(), timeout=5) == 1
    async with db.session() as s:
        job = await s.scalar(select(ScheduledJob))
    assert job is not None and job.status == "pending" and "no result in 0.2 s" in (job.last_error or "")
    assert worker.active == {}


async def test_own_running_job_is_renewed_not_reaped(db: Database, clock: FrozenClock) -> None:
    registry = HandlerRegistry()
    gate, running = asyncio.Event(), asyncio.Event()

    async def slow(ctx: JobContext) -> dict[str, Any]:
        running.set()
        await gate.wait()
        return {"done": True}

    registry.register("slow", slow)
    queue = JobQueue(db, clock, worker_id="w", lease_seconds=60)
    worker = Worker(queue, registry, _Container(db))  # type: ignore[arg-type]
    async with db.session() as s:
        await enqueue(s, job_type="slow", run_at=clock.now())
    run = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(running.wait(), 5)
    assert list(worker.active) != []
    clock.advance(timedelta(hours=1))  # the Mac slept: by the wall clock the lease ran out long ago
    assert await worker.reap() == {"requeued": 0, "dead": 0}
    gate.set()
    await run
    async with db.session() as s:
        job = await s.scalar(select(ScheduledJob))
    assert job is not None and job.status == "succeeded" and job.attempts == 1


async def test_a_slow_job_does_not_hold_back_the_next_one(db: Database, clock: FrozenClock) -> None:
    registry = HandlerRegistry()
    gate, running, fast_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    done: list[str] = []

    async def slow(ctx: JobContext) -> dict[str, Any]:
        running.set()
        await gate.wait()
        done.append("slow")
        return {}

    async def fast(ctx: JobContext) -> dict[str, Any]:
        done.append(str(ctx.payload["n"]))
        if len(done) == 3:
            fast_done.set()
        return {}

    registry.register("slow", slow)
    registry.register("fast", fast)
    queue = JobQueue(db, clock, worker_id="w")
    worker = Worker(queue, registry, _Container(db), concurrency=2, poll_interval=0.05)  # type: ignore[arg-type]
    async with db.session() as s:
        await enqueue(s, job_type="slow", run_at=clock.now())
    stop = asyncio.Event()
    loop = asyncio.create_task(worker.run_forever(stop))
    await asyncio.wait_for(running.wait(), 5)
    async with db.session() as s:  # a reminder becomes due while the slow job runs
        for n in range(3):
            await enqueue(s, job_type="fast", run_at=clock.now(), payload={"n": n})
    await asyncio.wait_for(fast_done.wait(), 5)
    assert sorted(done) == ["0", "1", "2"]  # all done while the slow one still runs
    gate.set()
    stop.set()
    await asyncio.wait_for(loop, timeout=5)
    assert done[-1] == "slow"


async def test_a_polling_job_is_skipped_while_the_provider_is_down(db: Database, clock: FrozenClock) -> None:
    # 16.10: no internet for four hours → 71 periodic reconciles retried into `dead`, all noise.
    registry = HandlerRegistry()

    async def down(ctx: JobContext) -> dict[str, Any]:
        raise CircuitOpenError("todoist: circuit open after repeated failures")

    registry.register("todoist.reconcile", down)
    worker = Worker(JobQueue(db, clock, worker_id="w", retry=NO_JITTER), registry, _Container(db))  # type: ignore[arg-type]
    async with db.session() as s:
        await enqueue(s, job_type="todoist.reconcile", run_at=clock.now(), dedupe_key="periodic:todoist.reconcile:1")
        await enqueue(s, job_type="todoist.reconcile", run_at=clock.now(), dedupe_key="by-hand")
    await worker.run_once()
    async with db.session() as s:
        jobs = {j.dedupe_key: j.status for j in (await s.scalars(select(ScheduledJob))).all()}
    assert jobs == {"periodic:todoist.reconcile:1": "skipped", "by-hand": "pending"}  # run by hand → retried
