"""Todoist → local cache synchronization.

Webhooks are notifications only: they enqueue `todoist.refresh_task`, which fetches the current task
through the API. `todoist.reconcile` periodically lists all active tasks and repairs anything missed.
Nothing in this module writes to Todoist.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.service import A, audit
from jarvis.core.errors import NotFoundError
from jarvis.core.security import verify_todoist_signature
from jarvis.db.models import IntegrationSyncState, TodoistTask, WebhookDelivery
from jarvis.integrations.todoist.client import TaskSnapshot, TodoistApi, parse_task
from jarvis.scheduler.queue import enqueue

log = logging.getLogger(__name__)

TASK_EVENTS = {"item:added", "item:updated", "item:deleted", "item:completed", "item:uncompleted"}


def _values(snapshot: TaskSnapshot, now: datetime) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "content": snapshot.content,
        "description": snapshot.description,
        "project_id": snapshot.project_id,
        "section_id": snapshot.section_id,
        "parent_id": snapshot.parent_id,
        "priority": snapshot.priority,
        "labels": list(snapshot.labels),
        "due_date": snapshot.due_date,
        "due_datetime": snapshot.due_datetime,
        "due_timezone": snapshot.due_timezone,
        "due_is_recurring": snapshot.due_is_recurring,
        "due_string": snapshot.due_string,
        "duration_minutes": snapshot.duration_minutes,
        "deadline_date": snapshot.deadline_date,
        "checked": snapshot.checked,
        "is_deleted": snapshot.is_deleted,
        "remote_updated_at": snapshot.updated_at,
        "synced_at": now,
    }


async def upsert_task(session: AsyncSession, snapshot: TaskSnapshot, now: datetime) -> bool:
    """Insert/update the cache. Older snapshots never overwrite newer ones (out-of-order safety)."""
    values = _values(snapshot, now)
    stmt = insert(TodoistTask).values(**values)
    update_cols = {k: stmt.excluded[k] for k in values if k != "id"}
    stale_guard = (
        (TodoistTask.remote_updated_at.is_(None))
        | (stmt.excluded.remote_updated_at.is_(None))
        | (stmt.excluded.remote_updated_at >= TodoistTask.remote_updated_at)
    )
    result = await session.execute(
        stmt.on_conflict_do_update(index_elements=[TodoistTask.id], set_=update_cols, where=stale_guard).returning(
            TodoistTask.id
        )
    )
    return result.first() is not None


async def mark_gone(session: AsyncSession, task_id: str, now: datetime, *, deleted: bool) -> None:
    values: dict[str, Any] = {"synced_at": now}
    values["is_deleted" if deleted else "checked"] = True
    await session.execute(update(TodoistTask).where(TodoistTask.id == task_id).values(**values))


class TodoistSync:
    def __init__(self, api: TodoistApi, tz: ZoneInfo) -> None:
        self.api = api
        self.tz = tz

    async def refresh_task(self, session: AsyncSession, task_id: str, now: datetime) -> str:
        try:
            raw = await self.api.get_task(task_id)
        except NotFoundError:
            await mark_gone(session, task_id, now, deleted=True)
            return "deleted"
        snapshot = parse_task(raw, self.tz)
        applied = await upsert_task(session, snapshot, now)
        return "updated" if applied else "stale_ignored"

    async def reconcile(self, session: AsyncSession, now: datetime) -> dict[str, int]:
        remote = [parse_task(raw, self.tz) for raw in await self.api.list_active_tasks()]
        remote_ids = {t.id for t in remote}
        # Only what changed is written: every two minutes every task used to be rewritten (L11, review 17.10).
        cached = {
            row.id: row
            for row in (await session.scalars(select(TodoistTask).where(TodoistTask.id.in_(remote_ids)))).all()
        }
        changed = 0
        for snapshot in remote:
            row = cached.get(snapshot.id)
            fields = {k: v for k, v in _values(snapshot, now).items() if k not in ("id", "synced_at")}
            if row is not None and all(getattr(row, k) == v for k, v in fields.items()):
                continue
            changed += int(await upsert_task(session, snapshot, now))
        local_active = (
            await session.scalars(
                select(TodoistTask.id).where(TodoistTask.checked.is_(False), TodoistTask.is_deleted.is_(False))
            )
        ).all()
        vanished = [task_id for task_id in local_active if task_id not in remote_ids]
        for task_id in vanished:
            # Not in the active list any more: completed or deleted. Ask the API which one.
            try:
                snapshot = parse_task(await self.api.get_task(task_id), self.tz)
                if not (snapshot.checked or snapshot.is_deleted):
                    snapshot = dataclasses.replace(snapshot, checked=True)
                await upsert_task(session, snapshot, now)
            except NotFoundError:
                await mark_gone(session, task_id, now, deleted=True)
        state = await session.scalar(
            select(IntegrationSyncState).where(
                IntegrationSyncState.provider == "todoist", IntegrationSyncState.resource == "tasks"
            )
        )
        if state is None:
            state = IntegrationSyncState(provider="todoist", resource="tasks", state={})
            session.add(state)
        state.last_success_at = now
        state.consecutive_failures = 0
        state.state = {"active": len(remote_ids), "vanished": len(vanished)}
        await audit(session, now=now, actor="sync", action=A.TASK_SYNCED, details=state.state)
        return {"active": len(remote_ids), "changed": changed, "vanished": len(vanished)}


class WebhookRejected(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


async def accept_todoist_webhook(
    session: AsyncSession,
    *,
    body: bytes,
    signature: str | None,
    delivery_id: str | None,
    client_secret: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Verify → dedupe → enqueue refresh. Returns a small status dict for the HTTP response."""
    if not client_secret:
        raise WebhookRejected(503, "todoist webhooks disabled (TODOIST_CLIENT_SECRET not set)")
    if not verify_todoist_signature(client_secret, body, signature):
        await audit(
            session,
            now=now,
            actor="todoist",
            action=A.WEBHOOK_REJECTED,
            result="denied",
            details={"reason": "bad signature"},
        )
        raise WebhookRejected(401, "invalid signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookRejected(400, "invalid json") from exc
    event = str(payload.get("event_name") or "")
    data = payload.get("event_data") or {}
    delivery = delivery_id or f"body:{hashlib.sha256(body).hexdigest()}"
    inserted = await session.execute(
        insert(WebhookDelivery)
        .values(provider="todoist", delivery_id=delivery[:200], event_name=event[:80], received_at=now)
        .on_conflict_do_nothing(index_elements=[WebhookDelivery.provider, WebhookDelivery.delivery_id])
        .returning(WebhookDelivery.id)
    )
    if inserted.first() is None:
        await audit(
            session, now=now, actor="todoist", action=A.WEBHOOK_DUPLICATE, result="duplicate", target_id=delivery[:100]
        )
        return {"status": "duplicate"}
    if event not in TASK_EVENTS or not data.get("id"):
        return {"status": "ignored", "event": event}
    task_id = str(data["id"])
    await enqueue(
        session,
        job_type="todoist.refresh_task",
        run_at=now,
        payload={"task_id": task_id, "event": event},
        dedupe_key=f"todoist:refresh:{task_id}:{delivery[:120]}",
        priority=50,
    )
    return {"status": "queued", "task_id": task_id}
