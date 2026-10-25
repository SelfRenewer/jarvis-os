"""Audit log for externally meaningful actions. Never store secrets in `details`."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.logging import REDACTOR, get_correlation_id
from jarvis.db.models import AuditEvent

log = logging.getLogger("jarvis.audit")

_SENSITIVE_KEYS = {"token", "secret", "password", "api_key", "authorization", "phone", "number", "cvv", "card"}


class A:
    """Audit action names."""

    TASK_UPDATED = "TASK_UPDATED"
    TASK_SYNCED = "TASK_SYNCED"
    CALENDAR_SYNCED = "CALENDAR_SYNCED"
    CALENDAR_EVENT_MIRRORED = "CALENDAR_EVENT_MIRRORED"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    PLAN_APPLIED = "PLAN_APPLIED"
    REMINDER_SCHEDULED = "REMINDER_SCHEDULED"
    REMINDER_CANCELLED = "REMINDER_CANCELLED"
    REMINDER_FIRED = "REMINDER_FIRED"
    REMINDER_MISSED = "REMINDER_MISSED"
    TELEGRAM_SENT = "TELEGRAM_SENT"
    VOICE_REPLY_FAILED = "VOICE_REPLY_FAILED"
    CALLBACK_REJECTED = "CALLBACK_REJECTED"
    TELEGRAM_UNAUTHORIZED = "TELEGRAM_UNAUTHORIZED"
    PHONE_CALL_REQUESTED = "PHONE_CALL_REQUESTED"
    PHONE_CALL_COMPLETED = "PHONE_CALL_COMPLETED"
    PHONE_CALL_FAILED = "PHONE_CALL_FAILED"
    PHONE_CALL_BLOCKED = "PHONE_CALL_BLOCKED"
    IDEA_CREATED = "IDEA_CREATED"
    IDEA_ANALYZED = "IDEA_ANALYZED"
    SPEC_CREATED = "SPEC_CREATED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    DEV_JOB_APPROVED = "DEV_JOB_APPROVED"
    DEV_JOB_CLAIMED = "DEV_JOB_CLAIMED"
    CLAUDE_RUN_STARTED = "CLAUDE_RUN_STARTED"
    CLAUDE_RUN_FINISHED = "CLAUDE_RUN_FINISHED"
    SHOPPING_ITEM_ADDED = "SHOPPING_ITEM_ADDED"
    SHOPPING_QUOTE_CREATED = "SHOPPING_QUOTE_CREATED"
    PURCHASE_APPROVED = "PURCHASE_APPROVED"
    PURCHASE_BLOCKED = "PURCHASE_BLOCKED"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED"
    WEBHOOK_DUPLICATE = "WEBHOOK_DUPLICATE"
    JOB_DEAD = "JOB_DEAD"
    INTEGRATION_AUTH_FAILED = "INTEGRATION_AUTH_FAILED"


def sanitize(details: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in details.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_KEYS):
            clean[key] = "***"
        elif isinstance(value, dict):
            clean[key] = sanitize(value)
        elif isinstance(value, str):
            clean[key] = REDACTOR.redact(value)[:2000]
        else:
            clean[key] = value
    return clean


async def audit(
    session: AsyncSession,
    *,
    now: datetime,
    actor: str,
    action: str,
    result: str = "ok",
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    event = AuditEvent(
        occurred_at=now,
        correlation_id=get_correlation_id(),
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        details=sanitize(details or {}),
    )
    session.add(event)
    log.info("audit %s %s %s:%s", action, result, target_type or "-", target_id or "-")
