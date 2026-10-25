"""Google Calendar → local cache (bounded-window reconciliation) + watch channels + Strategy-B mirror."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.audit.service import A, audit
from jarvis.core.config import CalendarStrategy, Settings
from jarvis.core.errors import PermanentError
from jarvis.core.security import constant_time_equals
from jarvis.db.models import (
    CalendarChannel,
    CalendarEvent,
    IntegrationSyncState,
    TodoistTask,
    WebhookDelivery,
)
from jarvis.integrations.google_calendar.client import CalendarApi, EventSnapshot, parse_event
from jarvis.scheduler.queue import enqueue_coalesced

log = logging.getLogger(__name__)

CHANNEL_TTL_SECONDS = 7 * 24 * 3600
RENEW_BEFORE = timedelta(hours=24)


def _event_values(snap: EventSnapshot, now: datetime) -> dict[str, Any]:
    return {
        "calendar_id": snap.calendar_id,
        "event_id": snap.event_id,
        "etag": snap.etag,
        "status": snap.status,
        "summary": snap.summary,
        "location": snap.location,
        "start_at": snap.start_at,
        "end_at": snap.end_at,
        "all_day": snap.all_day,
        "start_date": snap.start_date,
        "end_date": snap.end_date,
        "transparent": snap.transparent,
        "self_declined": snap.self_declined,
        "recurring_event_id": snap.recurring_event_id,
        "jarvis_managed": snap.jarvis_managed,
        "todoist_task_id": snap.todoist_task_id,
        "remote_updated_at": snap.updated,
        "synced_at": now,
    }


class CalendarSync:
    def __init__(self, api: CalendarApi, settings: Settings) -> None:
        self.api = api
        self.settings = settings
        self.tz: ZoneInfo = settings.tz

    async def resolve_excluded_calendars(self) -> set[str]:
        """Calendars never read as availability: the native Todoist calendar and the Jarvis mirror."""
        excluded: set[str] = set()
        if self.settings.google_todoist_calendar_id:
            excluded.add(self.settings.google_todoist_calendar_id)
        if self.settings.google_mirror_calendar_id:
            excluded.add(self.settings.google_mirror_calendar_id)
        if not self.settings.google_todoist_calendar_id:
            for cal in await self.api.list_calendars():
                if str(cal.get("summary", "")).strip().lower() == "todoist":
                    excluded.add(str(cal["id"]))
        return excluded

    async def reconcile(self, session: AsyncSession, now: datetime) -> dict[str, int]:
        window_start = now - timedelta(days=1)
        window_end = now + timedelta(days=self.settings.calendar_sync_days)
        excluded = await self.resolve_excluded_calendars()
        stats = {"changed": 0, "unchanged": 0, "removed": 0, "calendars": 0}
        for calendar_id in self.settings.google_calendar_ids:
            if calendar_id in excluded:
                continue
            stats["calendars"] += 1
            raw_events = await self.api.list_events(calendar_id, window_start, window_end)
            snaps = [parse_event(calendar_id, raw, self.tz) for raw in raw_events]
            seen: set[str] = {snap.event_id for snap in snaps}
            known = {  # one query for the calendar, not one per event (L12, review 17.10)
                row.event_id: (row.etag, row.status)
                for row in (
                    await session.execute(
                        select(CalendarEvent.event_id, CalendarEvent.etag, CalendarEvent.status).where(
                            CalendarEvent.calendar_id == calendar_id, CalendarEvent.event_id.in_(seen)
                        )
                    )
                ).all()
            }
            for snap in snaps:
                if known.get(snap.event_id) == (snap.etag, snap.status):
                    stats["unchanged"] += 1
                    continue
                values = _event_values(snap, now)
                stmt = insert(CalendarEvent).values(**values)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[CalendarEvent.calendar_id, CalendarEvent.event_id],
                        set_={k: stmt.excluded[k] for k in values if k not in ("calendar_id", "event_id")},
                    )
                )
                stats["changed"] += 1
            # Events inside the window that the API no longer returns were deleted outright.
            stale = (
                await session.scalars(
                    select(CalendarEvent).where(
                        CalendarEvent.calendar_id == calendar_id,
                        CalendarEvent.status != "cancelled",
                        CalendarEvent.start_at >= window_start,
                        CalendarEvent.start_at < window_end,
                    )
                )
            ).all()
            for event in stale:
                if event.event_id not in seen:
                    event.status = "cancelled"
                    event.synced_at = now
                    stats["removed"] += 1
        await self._mark_success(session, now, stats)
        await audit(session, now=now, actor="sync", action=A.CALENDAR_SYNCED, details=stats)
        if stats["changed"] or stats["removed"]:
            await enqueue_coalesced(session, job_type="reminders.plan", run_at=now, coalesce_key="calendar_changed")
        return stats

    async def _mark_success(self, session: AsyncSession, now: datetime, stats: dict[str, int]) -> None:
        state = await session.scalar(
            select(IntegrationSyncState).where(
                IntegrationSyncState.provider == "google_calendar", IntegrationSyncState.resource == "events"
            )
        )
        if state is None:
            state = IntegrationSyncState(provider="google_calendar", resource="events", state={})
            session.add(state)
        state.last_success_at = now
        state.consecutive_failures = 0
        state.state = dict(stats)

    # --- push channels ----------------------------------------------------------------------------

    async def ensure_channels(self, session: AsyncSession, now: datetime) -> dict[str, int]:
        base = self.settings.jarvis_public_base_url
        token = self.settings.google_channel_token
        if not base or not base.startswith("https://") or token is None:
            return {"skipped": 1}
        address = base.rstrip("/") + "/webhooks/google-calendar"
        created = renewed = 0
        excluded = await self.resolve_excluded_calendars()
        for calendar_id in self.settings.google_calendar_ids:
            if calendar_id in excluded:
                continue
            active = (
                await session.scalars(
                    select(CalendarChannel).where(
                        CalendarChannel.calendar_id == calendar_id, CalendarChannel.status == "active"
                    )
                )
            ).all()
            fresh = [c for c in active if c.expiration and c.expiration - now > RENEW_BEFORE]
            if fresh:
                continue
            channel_id = uuid.uuid4().hex
            data = await self.api.watch(calendar_id, channel_id, address, token.get_secret_value(), CHANNEL_TTL_SECONDS)
            expiration = None
            if data.get("expiration"):
                expiration = datetime.fromtimestamp(int(data["expiration"]) / 1000, UTC)
            session.add(
                CalendarChannel(
                    calendar_id=calendar_id,
                    channel_id=channel_id,
                    resource_id=data.get("resourceId"),
                    expiration=expiration,
                )
            )
            for old in active:
                old.status = "stopped"
                if old.resource_id:
                    await self.api.stop_channel(old.channel_id, old.resource_id)
                renewed += 1
            created += 1
        return {"created": created, "renewed": renewed}

    async def accept_notification(
        self, session: AsyncSession, headers: dict[str, str], now: datetime
    ) -> dict[str, str]:
        token = self.settings.google_channel_token
        if token is None:
            raise PermanentError("google push disabled")
        provided = headers.get("x-goog-channel-token", "")
        if not constant_time_equals(provided, token.get_secret_value()):
            raise PermissionError("bad channel token")
        channel_id = headers.get("x-goog-channel-id", "")
        channel = await session.scalar(select(CalendarChannel).where(CalendarChannel.channel_id == channel_id))
        if channel is None or channel.status != "active":
            return {"status": "unknown_channel"}
        message_no = headers.get("x-goog-message-number", "0")
        delivery = f"{channel_id}:{message_no}"
        inserted = await session.execute(
            insert(WebhookDelivery)
            .values(
                provider="google_calendar",
                delivery_id=delivery,
                event_name=headers.get("x-goog-resource-state"),
                received_at=now,
            )
            .on_conflict_do_nothing(index_elements=[WebhookDelivery.provider, WebhookDelivery.delivery_id])
            .returning(WebhookDelivery.id)
        )
        if inserted.first() is None:
            return {"status": "duplicate"}
        if headers.get("x-goog-resource-state") == "sync":
            return {"status": "sync_ack"}
        # Coalesce bursts: at most one pending early reconciliation at a time.
        await enqueue_coalesced(session, job_type="calendar.reconcile", run_at=now, coalesce_key="push", priority=60)
        return {"status": "queued"}


# --- Strategy B: one-way mirror Todoist → Google -----------------------------------------------------


def mirror_body(task: TodoistTask, tz: ZoneInfo) -> dict[str, Any] | None:
    if task.due_datetime is None or not task.duration_minutes or task.checked or task.is_deleted:
        return None
    end = task.due_datetime + timedelta(minutes=task.duration_minutes)
    return {
        "summary": task.content,
        "start": {"dateTime": task.due_datetime.isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(tz)},
        "extendedProperties": {"private": {"jarvis_managed": "true", "todoist_task_id": task.id}},
        "transparency": "opaque",
    }


def body_hash(body: dict[str, Any]) -> str:
    import json

    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


async def mirror_tasks(session: AsyncSession, api: CalendarApi, settings: Settings, now: datetime) -> dict[str, int]:
    """Write time-blocked Todoist tasks into the mirror calendar. Never reads anything back into Todoist."""
    if settings.calendar_strategy != CalendarStrategy.MIRROR or not settings.google_mirror_calendar_id:
        return {"skipped": 1}
    calendar_id = settings.google_mirror_calendar_id
    tasks = (
        await session.scalars(
            select(TodoistTask).where(
                and_(
                    TodoistTask.due_datetime.is_not(None),
                    TodoistTask.due_datetime >= now - timedelta(days=1),
                    TodoistTask.checked.is_(False),
                    TodoistTask.is_deleted.is_(False),
                )
            )
        )
    ).all()
    written = unchanged = 0
    for task in tasks:
        body = mirror_body(task, settings.tz)
        if body is None:
            continue
        digest = body_hash(body)
        state = await session.scalar(
            select(IntegrationSyncState).where(
                IntegrationSyncState.provider == "mirror", IntegrationSyncState.resource == f"task:{task.id}"
            )
        )
        if state is not None and state.cursor == digest:
            unchanged += 1
            continue
        existing = await api.find_mirror_event(calendar_id, task.id)
        result = await api.upsert_mirror_event(calendar_id, existing["id"] if existing else None, body)
        if state is None:
            state = IntegrationSyncState(provider="mirror", resource=f"task:{task.id}", state={})
            session.add(state)
        state.cursor = digest
        state.state = {"event_id": result.get("id")}
        state.last_success_at = now
        written += 1
        await audit(
            session,
            now=now,
            actor="mirror",
            action=A.CALENDAR_EVENT_MIRRORED,
            target_type="todoist_task",
            target_id=task.id,
        )
    return {"written": written, "unchanged": unchanged}
