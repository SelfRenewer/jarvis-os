"""Todoist cache, calendar cache, planner, reminders, travel estimates, phone calls."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, updated_at_col, uuid_pk


class TodoistTask(Base):
    """Local cache of Todoist tasks. Todoist remains the source of truth."""

    __tablename__ = "todoist_tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(String(40))
    section_id: Mapped[str | None] = mapped_column(String(40))
    parent_id: Mapped[str | None] = mapped_column(String(40))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    labels: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False, default=list)
    due_date: Mapped[date | None] = mapped_column(Date)
    due_datetime: Mapped[datetime | None] = mapped_column(UTCDateTime())
    due_timezone: Mapped[str | None] = mapped_column(String(60))
    due_is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    due_string: Mapped[str | None] = mapped_column(String(200))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    deadline_date: Mapped[date | None] = mapped_column(Date)
    checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    remote_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        Index("ix_todoist_tasks_active_due", "due_date", postgresql_where=text("NOT checked AND NOT is_deleted")),
    )


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    calendar_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_id: Mapped[str] = mapped_column(String(300), nullable=False)
    etag: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # confirmed|tentative|cancelled
    summary: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    transparent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    self_declined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurring_event_id: Mapped[str | None] = mapped_column(String(300))
    jarvis_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    todoist_task_id: Mapped[str | None] = mapped_column(String(40))
    remote_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("calendar_id", "event_id"),
        Index("ix_calendar_events_start", "start_at"),
    )


class CalendarChannel(Base):
    __tablename__ = "calendar_channels"

    id: Mapped[uuid.UUID] = uuid_pk()
    calendar_id: Mapped[str] = mapped_column(String(200), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(200))
    expiration: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active|stopped|expired
    created_at: Mapped[datetime] = created_at_col()


class PlannerRun(Base):
    __tablename__ = "planner_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    requested_by: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # succeeded|failed
    horizon_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    horizon_end: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class PlanningProposal(Base):
    __tablename__ = "planning_proposals"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planner_runs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auto_apply_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_col()
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PlanningProposalItem(Base):
    __tablename__ = "planning_proposal_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planning_proposals.id", ondelete="CASCADE"), index=True)
    todoist_task_id: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # schedule|keep|skip|unschedulable
    start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    previous: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    apply_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    apply_error: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)  # calendar_event|todoist_task
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    occurrence_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # leave|start
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # telegram|call
    fire_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scheduled_jobs.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (Index("ix_reminders_source", "source_type", "source_id"),)


class TravelEstimate(Base):
    __tablename__ = "travel_estimates"

    id: Mapped[uuid.UUID] = uuid_pk()
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    departure_bucket: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    travel_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # ok|not_found|unavailable|quota
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    static_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    traffic_aware: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CallRequest(Base):
    __tablename__ = "call_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    reminder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reminders.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    to_number_masked: Mapped[str] = mapped_column(String(30), nullable=False)
    requested_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # requesting|requested|in_progress|completed|failed|unknown|blocked|fallback_sent;
    # Telegram calls: queued → in_progress → completed|no_answer|declined|busy|failed, expired, unknown
    external_call_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    ended_reason: Mapped[str | None] = mapped_column(String(100))
    spoken_text: Mapped[str | None] = mapped_column(Text)  # Telegram calls: what the caller says
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        Index(
            "ix_call_requests_telegram_queued",
            "requested_for",
            postgresql_where=text("provider = 'telegram' AND status = 'queued'"),
        ),
    )
