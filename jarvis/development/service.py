"""Development jobs (server side).

Flow: spec → job(awaiting_approval) + approval → user approves → queued → Dev Worker *pulls* a signed
job → worker reports result. The server never pushes commands to the development machine.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.approvals.service import request_approval
from jarvis.audit.service import A, audit
from jarvis.core.errors import PermanentError
from jarvis.core.security import sign_payload
from jarvis.db.models import Approval, DevelopmentArtifact, DevelopmentJob, DevelopmentRun, DevSpec, Idea
from jarvis.ideas.spec import BRANCH_RE, branch_name_for

log = logging.getLogger(__name__)

JOB_LEASE = timedelta(hours=2)


class CheckResult(BaseModel):
    status: Literal["passed", "failed", "not_configured", "error", "timeout"]
    passed: int | None = None
    failed: int | None = None
    summary: str = Field(default="", max_length=2000)


class DevResult(BaseModel):
    """Machine-readable result reported by the Dev Worker (validated; worker input is untrusted)."""

    job_id: uuid.UUID
    status: Literal["succeeded", "failed", "rejected"]
    branch: str
    base_commit: str | None = None
    commit: str | None = None
    changed_files: int = 0
    diff_stat: str = Field(default="", max_length=20000)
    tests: CheckResult
    lint: CheckResult
    typecheck: CheckResult
    claude_status: Literal["success", "error", "not_run"]
    claude_cost_usd: float | None = None
    claude_turns: int | None = None
    requires_manual_review: list[str] = Field(default_factory=list)
    error: str | None = Field(default=None, max_length=4000)
    worker_version: str = "unknown"


def job_payload(job: DevelopmentJob, spec: DevSpec, now: datetime) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "repo_id": job.repo_id,
        "spec_id": str(spec.id),
        "branch_name": job.branch_name,
        "dev_task_markdown": spec.dev_task_md,
        "approved": True,
        "approval_id": str(job.approval_id),
        "issued_at": now.isoformat(),
    }


class DevelopmentService:
    def __init__(self, allowed_repo_ids: list[str], signing_key: str | None) -> None:
        self.allowed_repo_ids = allowed_repo_ids
        self.signing_key = signing_key

    async def request_job(
        self, session: AsyncSession, spec_id: uuid.UUID, repo_id: str, now: datetime
    ) -> tuple[DevelopmentJob, Approval]:
        if repo_id not in self.allowed_repo_ids:
            raise PermanentError(f"repository id {repo_id!r} is not in DEV_REPO_IDS")
        spec = await session.get(DevSpec, spec_id)
        if spec is None:
            raise LookupError("spec not found")
        existing = await session.scalar(
            select(DevelopmentJob).where(
                DevelopmentJob.spec_id == spec_id, DevelopmentJob.status.in_(["awaiting_approval", "queued", "running"])
            )
        )
        if existing is not None:
            approval = await session.get(Approval, existing.approval_id)
            assert approval is not None
            return existing, approval
        idea = await session.get(Idea, spec.idea_id)
        job_id = uuid.uuid4()
        branch = branch_name_for((idea.title if idea else None) or "task", job_id.hex)
        approval = await request_approval(
            session,
            kind="dev_job",
            target_type="development_job",
            target_id=str(job_id),
            now=now,
            ttl=timedelta(days=3),
            payload={"repo_id": repo_id, "branch": branch},
        )
        job = DevelopmentJob(
            id=job_id,
            spec_id=spec.id,
            repo_id=repo_id,
            branch_name=branch,
            status="awaiting_approval",
            approval_id=approval.id,
        )
        spec.repo_id = repo_id
        spec.status = "awaiting_approval"
        session.add(job)
        await session.flush()
        return job, approval

    async def on_decision(self, session: AsyncSession, approval: Approval, now: datetime) -> DevelopmentJob | None:
        job = await session.scalar(select(DevelopmentJob).where(DevelopmentJob.approval_id == approval.id))
        if job is None or job.status != "awaiting_approval":
            return job
        spec = await session.get(DevSpec, job.spec_id)
        if approval.status == "approved":
            job.status = "queued"
            job.approved_at = now
            if spec:
                spec.status = "approved"
            await audit(
                session,
                now=now,
                actor=approval.decided_by or "user",
                action=A.DEV_JOB_APPROVED,
                target_type="development_job",
                target_id=str(job.id),
                details={"repo_id": job.repo_id},
            )
        elif approval.status == "rejected":
            job.status = "rejected"
            if spec:
                spec.status = "rejected"
        return job

    async def claim(self, session: AsyncSession, worker_id: str, now: datetime) -> dict[str, Any] | None:
        if not self.signing_key:
            raise PermanentError("DEV_WORKER_SIGNING_KEY not configured")
        row = (
            await session.execute(
                text(
                    "SELECT id FROM development_jobs WHERE status = 'queued' AND approved_at IS NOT NULL "
                    "ORDER BY approved_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
            )
        ).first()
        if row is None:
            return None
        job = await session.get(DevelopmentJob, row.id)
        assert job is not None
        approval = await session.get(Approval, job.approval_id)
        if approval is None or approval.status != "approved":  # defense in depth
            job.status = "cancelled"
            return None
        spec = await session.get(DevSpec, job.spec_id)
        assert spec is not None
        if not BRANCH_RE.match(job.branch_name):
            job.status = "failed"
            job.result_summary = {"error": "invalid branch name"}
            return None
        job.status = "running"
        job.claimed_by = worker_id[:100]
        job.claimed_at = now
        job.lease_expires_at = now + JOB_LEASE
        payload = job_payload(job, spec, now)
        await audit(
            session,
            now=now,
            actor=f"dev_worker:{worker_id}",
            action=A.DEV_JOB_CLAIMED,
            target_type="development_job",
            target_id=str(job.id),
        )
        return {"job": payload, "signature": sign_payload(self.signing_key, payload)}

    async def heartbeat(self, session: AsyncSession, job_id: uuid.UUID, worker_id: str, now: datetime) -> bool:
        job = await session.get(DevelopmentJob, job_id, with_for_update=True)
        if job is None or job.status != "running" or job.claimed_by != worker_id:
            return False
        job.lease_expires_at = now + JOB_LEASE
        return True

    async def complete(self, session: AsyncSession, result: DevResult, worker_id: str, now: datetime) -> DevelopmentJob:
        job = await session.get(DevelopmentJob, result.job_id, with_for_update=True)
        if job is None:
            raise LookupError("job not found")
        if job.claimed_by != worker_id:
            raise PermissionError("job is claimed by another worker")
        if job.status != "running":
            return job  # duplicate report: idempotent no-op
        if result.branch != job.branch_name:
            raise PermanentError("worker reported a different branch than assigned")
        job.status = "succeeded" if result.status == "succeeded" else "failed"
        job.finished_at = now
        job.lease_expires_at = None
        summary = result.model_dump(mode="json")
        summary.pop("diff_stat", None)
        job.result_summary = summary
        session.add(
            DevelopmentRun(
                job_id=job.id,
                worker_id=worker_id,
                status=result.status,
                finished_at=now,
                run_metadata=result.model_dump(mode="json"),
            )
        )
        if result.diff_stat:
            session.add(
                DevelopmentArtifact(job_id=job.id, spec_id=job.spec_id, kind="diff_stat", content=result.diff_stat)
            )
        session.add(
            DevelopmentArtifact(job_id=job.id, spec_id=job.spec_id, kind="report", content=format_report(result))
        )
        await audit(
            session,
            now=now,
            actor=f"dev_worker:{worker_id}",
            action=A.CLAUDE_RUN_FINISHED,
            result="ok" if result.status == "succeeded" else "error",
            target_type="development_job",
            target_id=str(job.id),
            details={"tests": result.tests.status, "commit": result.commit or ""},
        )
        return job

    async def reap_lost(self, session: AsyncSession, now: datetime) -> int:
        rows = (
            await session.scalars(
                select(DevelopmentJob).where(DevelopmentJob.status == "running", DevelopmentJob.lease_expires_at < now)
            )
        ).all()
        for job in rows:
            job.status = "failed"
            job.finished_at = now
            job.result_summary = {
                "error": "worker lease expired (worker offline or crashed); not retried automatically"
            }
        return len(rows)


def format_report(result: DevResult) -> str:
    def check(name: str, c: CheckResult) -> str:
        counts = ""
        if c.passed is not None or c.failed is not None:
            counts = f" ({c.passed or 0} passed, {c.failed or 0} failed)"
        return f"{name}: {c.status}{counts}"

    title = "✅ Development completed" if result.status == "succeeded" else "❌ Development failed"
    lines = [
        title,
        f"Branch: {result.branch}",
        f"Changed: {result.changed_files} files",
        check("Tests", result.tests),
        check("Lint", result.lint),
        check("Type check", result.typecheck),
        f"Claude run: {result.claude_status}" + (f" (${result.claude_cost_usd:.2f})" if result.claude_cost_usd else ""),
    ]
    if result.requires_manual_review:
        lines.append("Requires manual review: " + ", ".join(result.requires_manual_review))
    if result.commit:
        lines.append(f"Commit: {result.commit[:12]}")
    if result.error:
        lines.append(f"Error: {result.error[:500]}")
    lines.append("Nothing was merged or deployed. Review the branch manually.")
    return "\n".join(lines)
