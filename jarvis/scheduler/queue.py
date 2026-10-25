"""Durable PostgreSQL job queue.

Invariants:
* a job row is the single source of truth for future work;
* `dedupe_key` is globally unique → repeated enqueue of the same logical job is a no-op;
* claiming uses `FOR UPDATE SKIP LOCKED` → concurrent workers never claim the same job;
* each claim holds a lease; finishing requires still owning the lease (guarded UPDATE);
* expired leases are reaped (crash recovery) back to `pending` or to `dead`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.clock import Clock, ensure_aware
from jarvis.core.retry import JOB_RETRY, RetryPolicy
from jarvis.db.models import JobAttempt, ScheduledJob
from jarvis.db.session import Database

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
DEAD = "dead"
CANCELLED = "cancelled"
SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    job_type: str
    payload: dict[str, Any]
    run_at: datetime
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    correlation_id: str | None


async def enqueue(
    session: AsyncSession,
    *,
    job_type: str,
    run_at: datetime,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = JOB_RETRY.max_attempts,
    priority: int = 100,
    correlation_id: str | None = None,
) -> uuid.UUID | None:
    """Insert a job. Returns the new id, or None when `dedupe_key` already exists."""
    ensure_aware(run_at)
    job_id = uuid.uuid4()
    stmt = (
        insert(ScheduledJob)
        .values(
            id=job_id,
            job_type=job_type,
            run_at=run_at,
            payload=payload or {},
            status=PENDING,
            priority=priority,
            attempts=0,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
        )
        .on_conflict_do_nothing(index_elements=[ScheduledJob.dedupe_key])
        .returning(ScheduledJob.id)
    )
    result = await session.execute(stmt)
    row = result.first()
    return row[0] if row else None


async def enqueue_coalesced(
    session: AsyncSession,
    *,
    job_type: str,
    run_at: datetime,
    coalesce_key: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
) -> uuid.UUID | None:
    """Enqueue unless an equivalent job is still *pending*.

    Unlike `dedupe_key` (unique forever), this only merges bursts: once the pending job has run, the next
    trigger creates a new job. Only for idempotent job types (a rare concurrent duplicate is harmless).
    """
    pending = await session.scalar(
        select(ScheduledJob.id)
        .where(
            ScheduledJob.job_type == job_type,
            ScheduledJob.status == PENDING,
            ScheduledJob.payload["coalesce"].astext == coalesce_key,
        )
        .limit(1)
    )
    if pending is not None:
        return None
    return await enqueue(
        session,
        job_type=job_type,
        run_at=run_at,
        payload={**(payload or {}), "coalesce": coalesce_key},
        priority=priority,
    )


async def cancel_job(session: AsyncSession, job_id: uuid.UUID, reason: str) -> bool:
    result = await session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id, ScheduledJob.status == PENDING)
        .values(status=CANCELLED, last_error=reason[:500])
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def reschedule_job(session: AsyncSession, job_id: uuid.UUID, run_at: datetime) -> bool:
    result = await session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id, ScheduledJob.status == PENDING)
        .values(run_at=ensure_aware(run_at))
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


class JobQueue:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        retry: RetryPolicy = JOB_RETRY,
    ) -> None:
        self.db = db
        self.clock = clock
        self.worker_id = worker_id
        self.lease = timedelta(seconds=lease_seconds)
        self.retry = retry

    async def claim(self, limit: int = 1) -> list[ClaimedJob]:
        now = self.clock.now()
        async with self.db.session() as session:
            due = (
                select(ScheduledJob.id)
                .where(ScheduledJob.status == PENDING, ScheduledJob.run_at <= now)
                .order_by(ScheduledJob.priority, ScheduledJob.run_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            stmt = (
                update(ScheduledJob)
                .where(ScheduledJob.id.in_(due.scalar_subquery()))
                .values(
                    status=RUNNING,
                    locked_at=now,
                    locked_by=self.worker_id,
                    lock_expires_at=now + self.lease,
                    attempts=ScheduledJob.attempts + 1,
                )
                .returning(
                    ScheduledJob.id,
                    ScheduledJob.job_type,
                    ScheduledJob.payload,
                    ScheduledJob.run_at,
                    ScheduledJob.attempts,
                    ScheduledJob.max_attempts,
                    ScheduledJob.dedupe_key,
                    ScheduledJob.correlation_id,
                )
                .execution_options(synchronize_session=False)
            )
            rows = (await session.execute(stmt)).all()
            claimed = [
                ClaimedJob(
                    id=row.id,
                    job_type=row.job_type,
                    payload=dict(row.payload or {}),
                    run_at=row.run_at,
                    attempts=row.attempts,
                    max_attempts=row.max_attempts,
                    dedupe_key=row.dedupe_key,
                    correlation_id=row.correlation_id,
                )
                for row in rows
            ]
            for job in claimed:
                session.add(
                    JobAttempt(
                        job_id=job.id,
                        attempt_no=job.attempts,
                        worker_id=self.worker_id,
                        started_at=now,
                        status=RUNNING,
                    )
                )
        return claimed

    def _owned(self, job_id: uuid.UUID) -> Any:
        return and_(
            ScheduledJob.id == job_id,
            ScheduledJob.status == RUNNING,
            ScheduledJob.locked_by == self.worker_id,
        )

    async def _finish_attempt(
        self, session: AsyncSession, job: ClaimedJob, status: str, error: str | None, now: datetime
    ) -> None:
        attempt = await session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempts)
        )
        if attempt is not None:
            attempt.status = status
            attempt.error = error[:2000] if error else None
            attempt.finished_at = now
            attempt.duration_ms = int((now - attempt.started_at).total_seconds() * 1000)

    async def complete(self, job: ClaimedJob, result: dict[str, Any] | None = None, *, status: str = SUCCEEDED) -> bool:
        now = self.clock.now()
        async with self.db.session() as session:
            res = await session.execute(
                update(ScheduledJob)
                .where(self._owned(job.id))
                .values(
                    status=status,
                    result=result,
                    finished_at=now,
                    lock_expires_at=None,
                    last_error=None if status == SUCCEEDED else (result or {}).get("reason"),
                )
            )
            await self._finish_attempt(session, job, status, None, now)
            return bool(res.rowcount)  # type: ignore[attr-defined]

    async def fail(
        self,
        job: ClaimedJob,
        error: str,
        *,
        permanent: bool,
        retry_after_seconds: float | None = None,
    ) -> str:
        """Record a failure. Returns the new status (`pending` for retry, `dead` otherwise)."""
        now = self.clock.now()
        exhausted = job.attempts >= job.max_attempts
        async with self.db.session() as session:
            if permanent or exhausted:
                new_status = DEAD
                values: dict[str, Any] = {"status": DEAD, "finished_at": now}
            else:
                new_status = PENDING
                delay = self.retry.delay(job.attempts, str(job.id))
                if retry_after_seconds is not None:
                    delay = max(delay, min(retry_after_seconds, self.retry.cap_seconds))
                values = {"status": PENDING, "run_at": now + timedelta(seconds=delay)}
            values.update(last_error=error[:2000], locked_by=None, locked_at=None, lock_expires_at=None)
            await session.execute(update(ScheduledJob).where(self._owned(job.id)).values(**values))
            await self._finish_attempt(session, job, "dead" if new_status == DEAD else "retry", error, now)
        return new_status

    async def extend_lease(self, job: ClaimedJob) -> bool:
        now = self.clock.now()
        async with self.db.session() as session:
            res = await session.execute(
                update(ScheduledJob).where(self._owned(job.id)).values(lock_expires_at=now + self.lease)
            )
            return bool(res.rowcount)  # type: ignore[attr-defined]

    async def reap_expired_leases(self) -> dict[str, int]:
        """Crash recovery: running jobs whose lease expired go back to pending (or dead)."""
        now = self.clock.now()
        async with self.db.session() as session:
            requeued = await session.execute(
                text(
                    """
                    UPDATE scheduled_jobs
                       SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
                           last_error = 'lease expired (worker crashed or stalled)',
                           locked_by = NULL, locked_at = NULL, lock_expires_at = NULL,
                           finished_at = CASE WHEN attempts >= max_attempts THEN :now ELSE NULL END,
                           updated_at = :now
                     WHERE status = 'running' AND lock_expires_at < :now
                 RETURNING id, status
                    """
                ),
                {"now": now},
            )
            rows = requeued.all()
            if rows:
                await session.execute(
                    text(
                        "UPDATE job_attempts SET status = 'crashed', finished_at = :now "
                        "WHERE status = 'running' AND job_id = ANY(:ids)"
                    ),
                    {"now": now, "ids": [row.id for row in rows]},
                )
        return {
            "requeued": sum(1 for row in rows if row.status == PENDING),
            "dead": sum(1 for row in rows if row.status == DEAD),
        }

    async def stats(self) -> dict[str, int]:
        async with self.db.session() as session:
            rows = (
                await session.execute(text("SELECT status, count(*) AS n FROM scheduled_jobs GROUP BY status"))
            ).all()
        return {row.status: int(row.n) for row in rows}
