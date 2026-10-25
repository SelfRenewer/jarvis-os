"""Watch and scale data from the phone: Health Connect → Life Dashboard Companion → POST /webhooks/health.

Payloads are stored as received and processed by a job; readings are kept per Health Connect record, so a record that
arrives again (a retry, a backfill, an edit at the source) is stored once.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, updated_at_col, uuid_pk


class HealthPayload(Base):
    """One request from the phone, kept until `health.ingest` has read it. `sequence` only grows per phone install:
    a payload sent twice has the same number and is stored once."""

    __tablename__ = "health_payloads"

    id: Mapped[uuid.UUID] = uuid_pk()
    sequence: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = created_at_col()
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (Index("ix_health_payloads_pending", "received_at", postgresql_where="processed_at IS NULL"),)


class HealthSample(Base):
    """One reading: a sleep session (stages in `data`), a heart-rate sample, a weighing, steps of an hour…

    `record_id` is Health Connect's id (samples of one record: `<id>#<epoch ms>`). `value2` is the second number of
    a pair (blood pressure: diastolic)."""

    __tablename__ = "health_samples"

    id: Mapped[uuid.UUID] = uuid_pk()
    record_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str | None] = mapped_column(String(160))  # the app that wrote it to Health Connect
    start_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    value: Mapped[float | None] = mapped_column(Float)
    value2: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("ix_health_samples_kind_start_at", "kind", "start_at"),)


class HealthDay(Base):
    """Daily totals as Health Connect deduplicated them (phone and watch counting the same steps count once).
    Today's row grows with every sync; a row is only replaced by a payload with a higher `sequence`."""

    __tablename__ = "health_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    steps: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[float | None] = mapped_column(Float)
    active_kcal: Mapped[float | None] = mapped_column(Float)
    total_kcal: Mapped[float | None] = mapped_column(Float)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = updated_at_col()


class ScreenDay(Base):
    """Phone screen time of a day (Life Dashboard «Screen Time», the day from 04:00 by default). The newest payload
    (highest `sequence`) wins for a date: every sync sends the last 7 days again."""

    __tablename__ = "screen_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    apps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)  # top apps by minutes
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = updated_at_col()
