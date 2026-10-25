"""Actions for the Jarvis app on the user's Mac (pull model: the app claims them over the local API)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, uuid_pk


class MacAction(Base):
    """One thing to do on the Mac: open an app / a site / a folder, start Claude Code in a project.

    `kind` is a closed set and `payload` holds values Jarvis resolved itself (an installed app's name, a project
    path the Mac reported); the app validates both again before acting. Statuses: proposed (waits for the user's
    confirmation button) → pending → claimed → done | failed; pending ones expire (no surprise launches hours
    later when the Mac wakes up)."""

    __tablename__ = "mac_actions"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # desktop | telegram
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (Index("ix_mac_actions_pending", "created_at", postgresql_where="status = 'pending'"),)
