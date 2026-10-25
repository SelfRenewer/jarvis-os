"""Job execution loop and handler registry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from jarvis.core.errors import CircuitOpenError, JarvisError, PermanentError, TransientError
from jarvis.core.logging import correlation_id_var, log_extra, new_correlation_id
from jarvis.scheduler.queue import SKIPPED, ClaimedJob, JobQueue

if TYPE_CHECKING:
    from jarvis.services.container import Container

log = logging.getLogger(__name__)


class SkipJob(JarvisError):
    """Raised by a handler when the job is no longer meaningful (e.g. event cancelled, too late)."""

    def __init__(self, reason: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.result = result or {}


@dataclass(slots=True)
class JobContext:
    job: ClaimedJob
    now: datetime
    container: Container

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload

    @property
    def lateness(self) -> timedelta:
        return self.now - self.job.run_at


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]

# A handler that never returns used to hold its worker slot — and the whole batch, the lease reaper and every
# later reminder — forever. Each job now has a time limit (then it is retried like any transient failure) and its
# lease is renewed while it runs, so a long but alive job is not handed to another worker.
DEFAULT_JOB_TIMEOUT = 180.0
JOB_TIMEOUTS: dict[str, float] = {
    "embeddings.backfill": 600.0,
    "health.ingest": 600.0,
    "tasks.apply_op": 600.0,
    "research.run": 300.0,
    "planner.apply": 300.0,
    "calendar.mirror": 300.0,
}


# Polling jobs whose next periodic run does the same work: with the provider's circuit open (the Mac lost the
# internet) they are skipped instead of retried into `dead` (16.10: 71 dead jobs in four hours, all noise).
CATCH_UP_NEXT_RUN = frozenset(
    {"todoist.reconcile", "calendar.reconcile", "usage.snapshot", "weather.watch", "embeddings.backfill"}
)


class JobTimeoutError(TransientError):
    """The handler ran past its time limit and was cancelled."""


@dataclass
class HandlerRegistry:
    handlers: dict[str, Handler] = field(default_factory=dict)

    def register(self, job_type: str, handler: Handler) -> None:
        if job_type in self.handlers:
            raise ValueError(f"duplicate handler for {job_type}")
        self.handlers[job_type] = handler

    def get(self, job_type: str) -> Handler | None:
        return self.handlers.get(job_type)


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        registry: HandlerRegistry,
        container: Container,
        *,
        concurrency: int = 4,
        poll_interval: float = 2.0,
        timeouts: dict[str, float] | None = None,
    ) -> None:
        self.queue = queue
        self.registry = registry
        self.container = container
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.timeouts = JOB_TIMEOUTS if timeouts is None else timeouts
        self.active: dict[Any, ClaimedJob] = {}  # running here: their leases are renewed, never reaped by us

    def timeout_for(self, job_type: str) -> float:
        return self.timeouts.get(job_type, DEFAULT_JOB_TIMEOUT)

    async def _keep_lease(self, job: ClaimedJob) -> None:
        every = max(1.0, self.queue.lease.total_seconds() / 3)
        while True:
            await asyncio.sleep(every)
            try:
                if not await self.queue.extend_lease(job):
                    log.warning("lost the lease of %s %s", job.job_type, job.id)
                    return
            except Exception:  # the next round tries again; the reaper skips our own jobs anyway
                log.exception("lease renewal failed for %s", job.job_type)

    async def execute(self, job: ClaimedJob) -> str:
        token = correlation_id_var.set(job.correlation_id or new_correlation_id())
        started = time.monotonic()
        try:
            handler = self.registry.get(job.job_type)
            if handler is None:
                await self.queue.fail(job, f"no handler for job type {job.job_type}", permanent=True)
                return "dead"
            ctx = JobContext(job=job, now=self.queue.clock.now(), container=self.container)
            limit = self.timeout_for(job.job_type)
            self.active[job.id] = job
            renew = asyncio.create_task(self._keep_lease(job))
            try:
                try:
                    result = await asyncio.wait_for(handler(ctx), timeout=limit)
                except TimeoutError as exc:
                    raise JobTimeoutError(f"{job.job_type}: no result in {limit:g} s") from exc
                finally:
                    renew.cancel()
                    self.active.pop(job.id, None)
            except SkipJob as skip:
                await self.queue.complete(job, {"reason": skip.reason, **skip.result}, status=SKIPPED)
                log.info("job skipped %s: %s", job.job_type, skip.reason)
                return SKIPPED
            except PermanentError as exc:
                status = await self.queue.fail(job, f"{type(exc).__name__}: {exc}", permanent=True)
                await self._on_dead(job, exc)
                return status
            except TransientError as exc:
                if (
                    isinstance(exc, CircuitOpenError)
                    and job.job_type in CATCH_UP_NEXT_RUN
                    and (job.dedupe_key or "").startswith("periodic:")
                ):
                    await self.queue.complete(job, {"reason": f"provider down, the next run catches up: {exc}"},
                                              status=SKIPPED)  # fmt: skip
                    log.info("job skipped %s: %s", job.job_type, exc)
                    return SKIPPED
                status = await self.queue.fail(
                    job, f"{type(exc).__name__}: {exc}", permanent=False, retry_after_seconds=exc.retry_after_seconds
                )
                if status == "dead":
                    await self._on_dead(job, exc)
                return status
            except Exception as exc:  # unknown failure → treat as transient, bounded by max_attempts
                log.exception("job %s crashed", job.job_type)
                status = await self.queue.fail(job, f"{type(exc).__name__}: {exc}", permanent=False)
                if status == "dead":
                    await self._on_dead(job, exc)
                return status
            await self.queue.complete(job, result or {})
            log.info(
                "job done %s",
                job.job_type,
                extra=log_extra(job_id=str(job.id), ms=int((time.monotonic() - started) * 1000)),
            )
            return "succeeded"
        finally:
            correlation_id_var.reset(token)

    async def _on_dead(self, job: ClaimedJob, exc: BaseException) -> None:
        log.error("job dead %s %s: %s", job.job_type, job.id, exc)
        try:
            from jarvis.audit.service import A, audit

            async with self.container.db.session() as session:
                await audit(
                    session,
                    now=self.queue.clock.now(),
                    actor="worker",
                    action=A.JOB_DEAD,
                    result="error",
                    target_type="job",
                    target_id=str(job.id),
                    details={"job_type": job.job_type, "error": str(exc)[:500], "attempts": job.attempts},
                )
        except Exception:  # pragma: no cover - auditing must never crash the worker
            log.exception("failed to audit dead job")

    async def run_once(self, limit: int | None = None) -> int:
        jobs = await self.queue.claim(limit or self.concurrency)
        if not jobs:
            return 0
        await asyncio.gather(*(self.execute(job) for job in jobs))
        return len(jobs)

    async def drain(self, max_rounds: int = 50) -> int:
        """Run until nothing is due (tests / one-shot CLI)."""
        total = 0
        for _ in range(max_rounds):
            count = await self.run_once()
            if count == 0:
                break
            total += count
        return total

    async def reap(self) -> dict[str, int]:
        """Our own running jobs first get their lease renewed (after the Mac slept, every lease has expired by the
        wall clock), then expired leases of crashed workers go back to the queue."""
        for job in list(self.active.values()):
            with contextlib.suppress(Exception):
                await self.queue.extend_lease(job)
        return await self.queue.reap_expired_leases()

    async def _reap_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                reaped = await self.reap()
                if any(reaped.values()):
                    log.warning("reaped expired leases %s", reaped)
            except Exception:
                log.exception("lease reaper failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=30)

    async def run_forever(self, stop: asyncio.Event, heartbeat: Callable[[], Awaitable[None]] | None = None) -> None:
        """Up to `concurrency` jobs at once; a free slot takes the next due job at once (a slow job no longer holds
        back the reminders claimed with it). The lease reaper runs on its own."""
        running: set[asyncio.Task[str]] = set()
        reaper = asyncio.create_task(self._reap_forever(stop))
        last_beat = 0.0
        try:
            while not stop.is_set():
                claimed: list[ClaimedJob] = []
                try:
                    if heartbeat and time.monotonic() - last_beat > 15:
                        await heartbeat()
                        last_beat = time.monotonic()
                    free = self.concurrency - len(running)
                    if free > 0:
                        claimed = await self.queue.claim(free)
                except Exception:
                    log.exception("worker loop error")
                for job in claimed:
                    task = asyncio.create_task(self.execute(job))
                    running.add(task)
                    task.add_done_callback(running.discard)
                if claimed and len(running) < self.concurrency:
                    continue  # maybe more is due
                if running and len(running) >= self.concurrency:
                    await asyncio.wait(running, timeout=self.poll_interval, return_when=asyncio.FIRST_COMPLETED)
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
        finally:
            if running:  # stopping: let the jobs in hand finish (each is bounded by its time limit)
                await asyncio.gather(*running, return_exceptions=True)
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
