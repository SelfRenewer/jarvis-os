"""AI Inbox: notes and pending button choices (inbox routing, task disambiguation)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Computed, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, uuid_pk


class Note(Base):
    """Information the user wants to keep (a code, an address, a fact). Stored verbatim."""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = uuid_pk()
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # telegram_text|telegram_inbox|api
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = created_at_col()
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', coalesce(text, ''))", persisted=True)
    )

    __table_args__ = (Index("ix_notes_text_tsv", "text_tsv", postgresql_using="gin"),)


class PendingChoice(Base):
    """A question asked with inline buttons. Resolved at most once (single conditional UPDATE).

    `payload` holds only data Jarvis produced itself (the original message, candidate task ids found in SQL);
    a button carries just the choice id and an option index.
    """

    __tablename__ = "pending_choices"

    id: Mapped[uuid.UUID] = uuid_pk()
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # inbox|task_pick
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|resolved
    resolution: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProfileFact(Base):
    """What Jarvis knows about the user (2026-10-14): said explicitly — «запомни, что мой барбер — Гоги» — and given
    to the router and the question agent as context. Never taken from forwarded messages, web pages or photos.
    «забудь …» marks it forgotten (kept for the audit trail, never shown or used again)."""

    __tablename__ = "profile_facts"

    id: Mapped[uuid.UUID] = uuid_pk()
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")  # active|forgotten
    created_at: Mapped[datetime] = created_at_col()
    forgotten_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (Index("ix_profile_facts_active", "created_at", postgresql_where="status = 'active'"),)
