"""OpenRouter spend history: the key's cumulative usage at points in time (money as integer micro-dollars)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime


class SpendSnapshot(Base):
    """Cumulative spend of the Jarvis key at `taken_at`. OpenRouter keeps no history a normal key can read, so
    Jarvis samples `usage` hourly; spend over a period = now − the sample at its start.

    `source`: "key" (sampled at that moment) or "day"/"week"/"month" (derived from the key's calendar counters:
    usage − usage_daily is the cumulative value at the start of the UTC day, and so on)."""

    __tablename__ = "spend_snapshots"

    taken_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    usage_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 1 USD = 1 000 000
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="key")
