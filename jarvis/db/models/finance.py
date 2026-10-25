"""Expenses the user reports («потратил 45 лари на такси», a receipt photo). Money: integer minor units."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, uuid_pk


class Expense(Base):
    """One expense. `amount_minor` × 10^-exponent(currency) = the amount; never a float.

    `source_ref` makes a retried message / a double-tapped button a no-op. Removal is soft (`deleted_at`): the
    undo button and «удали последний расход» only hide the row."""

    __tablename__ = "expenses"

    id: Mapped[uuid.UUID] = uuid_pk()
    spent_on: Mapped[date] = mapped_column(Date, nullable=False)  # the user's local day
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # text | voice | receipt
    source_ref: Mapped[str | None] = mapped_column(String(100), unique=True)
    raw_media_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_media.id"))
    created_at: Mapped[datetime] = created_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        Index("ix_expenses_spent_on", "spent_on", postgresql_where="deleted_at IS NULL"),
        CheckConstraint("amount_minor > 0", name="positive_amount"),
    )
