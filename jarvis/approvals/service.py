"""Human approval boundary.

`decide()` is a single conditional UPDATE: only the first decision on a pending approval takes effect,
so duplicate Telegram callbacks / double taps are harmless no-ops.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.service import A, audit
from jarvis.db.models import Approval

APPROVE = "approved"
REJECT = "rejected"


@dataclass(frozen=True)
class Decision:
    approval: Approval
    changed: bool  # False when it was already decided/expired (duplicate callback)


async def request_approval(
    session: AsyncSession,
    *,
    kind: str,
    target_type: str,
    target_id: str,
    now: datetime,
    ttl: timedelta | None = timedelta(days=2),
    payload: dict[str, Any] | None = None,
) -> Approval:
    approval = Approval(
        kind=kind,
        target_type=target_type,
        target_id=target_id,
        nonce=secrets.token_urlsafe(9),
        status="pending",
        requested_at=now,
        expires_at=(now + ttl) if ttl else None,
        payload=payload or {},
    )
    session.add(approval)
    await session.flush()
    await audit(
        session,
        now=now,
        actor="jarvis",
        action=A.APPROVAL_REQUESTED,
        target_type=kind,
        target_id=str(approval.id),
        details={"target": f"{target_type}:{target_id}"},
    )
    return approval


async def decide(session: AsyncSession, *, nonce: str, decision: str, actor: str, now: datetime) -> Decision | None:
    if decision not in (APPROVE, REJECT):
        raise ValueError("invalid decision")
    result = await session.execute(
        update(Approval)
        .where(
            Approval.nonce == nonce,
            Approval.status == "pending",
            (Approval.expires_at.is_(None)) | (Approval.expires_at > now),
        )
        .values(status=decision, decided_at=now, decided_by=actor)
        .returning(Approval.id)
    )
    changed = result.first() is not None
    approval = await session.scalar(
        select(Approval).where(Approval.nonce == nonce).execution_options(populate_existing=True)
    )
    if approval is None:
        return None
    if not changed and approval.status == "pending" and approval.expires_at and approval.expires_at <= now:
        approval.status = "expired"
    if changed:
        await audit(
            session,
            now=now,
            actor=actor,
            action=A.APPROVAL_DECIDED,
            target_type=approval.kind,
            target_id=str(approval.id),
            details={"decision": decision},
        )
    return Decision(approval=approval, changed=changed)
