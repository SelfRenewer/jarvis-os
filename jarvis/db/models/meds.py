"""Medication / supplement reminders: what to take and when, and each intake's outcome."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, updated_at_col, uuid_pk


class Medication(Base):
    """A course set by the user («Омега-3, 1 капсула, 09:00 и 21:00»). Jarvis only reminds — never advises."""

    __tablename__ = "medications"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dose: Mapped[str | None] = mapped_column(Text)
    times: Mapped[list[str]] = mapped_column(ARRAY(String(5)), nullable=False)  # local "HH:MM", sorted
    # What is taken, for TLT: [{"said", "supplement" (LMF name or null), "label", "dose", "unit", "dose_text"}].
    # Several items = a pack («Утренние»: D3, B12, цинк) — one reminder for all of them.
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # A course with an end (a doctor's plan: «курсом 8 недель»): the last local day with intakes; then the
    # course is closed (active = False) with a message and can be extended («продли магний на 2 недели»).
    ends_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class MedicationIntake(Base):
    """One scheduled intake. Created when it becomes due; its status changes at most once from `pending`."""

    __tablename__ = "medication_intakes"

    id: Mapped[uuid.UUID] = uuid_pk()
    medication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medications.id", ondelete="CASCADE"))
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|taken|skipped|missed
    reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    nudged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    items_taken: Mapped[list[int] | None] = mapped_column(JSONB)  # «☑ Не всё»: indexes of the items taken

    __table_args__ = (
        UniqueConstraint("medication_id", "due_at"),
        Index("ix_medication_intakes_pending", "due_at", postgresql_where="status = 'pending'"),
    )
