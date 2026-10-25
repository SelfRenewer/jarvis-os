"""Ideas, raw media, semantic memory and development jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, updated_at_col, uuid_pk

EMBEDDING_DIMS = 1024


class RawMedia(Base):
    __tablename__ = "raw_media"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # voice
    mime_type: Mapped[str | None] = mapped_column(String(60))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = created_at_col()


class Idea(Base):
    """Original thought. `raw_input` is immutable (enforced by a DB trigger)."""

    __tablename__ = "ideas"

    id: Mapped[uuid.UUID] = uuid_pk()
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # telegram_text|telegram_voice|api
    raw_input: Mapped[str] = mapped_column(Text, nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text)
    raw_media_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_media.id"))
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str | None] = mapped_column(Text)
    analysis_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = created_at_col()
    raw_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('russian', coalesce(raw_input, '') || ' ' || coalesce(transcript, ''))", persisted=True),
    )

    __table_args__ = (Index("ix_ideas_raw_tsv", "raw_tsv", postgresql_using="gin"),)


class IdeaVersion(Base):
    __tablename__ = "idea_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    idea_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ideas.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    problem: Mapped[str | None] = mapped_column(Text)
    proposed_solution: Mapped[str | None] = mapped_column(Text)
    user_value: Mapped[str | None] = mapped_column(Text)
    assumptions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    open_questions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    constraints: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    related_projects: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    tags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    produced_by: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || "
            "coalesce(problem, '') || ' ' || coalesce(proposed_solution, ''))",
            persisted=True,
        ),
    )

    __table_args__ = (
        UniqueConstraint("idea_id", "version"),
        Index("ix_idea_versions_search_tsv", "search_tsv", postgresql_using="gin"),
    )


class MemoryEmbedding(Base):
    """One vector per (object, model) for search by meaning (`jarvis/search/service.py`).

    `object_id` is text: Todoist task ids are strings, the other objects are UUIDs. `content_hash` is the sha256 of
    the embedded text — an unchanged object is never embedded again. Vectors of different models are never compared.
    """

    __tablename__ = "memory_embeddings"

    id: Mapped[uuid.UUID] = uuid_pk()
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)  # idea|note|task|event|shopping
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMS), nullable=False)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        UniqueConstraint("object_type", "object_id", "model"),
        Index("ix_memory_embeddings_model_type", "model", "object_type"),
        Index(
            "ix_memory_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class DevSpec(Base):
    __tablename__ = "dev_specs"

    id: Mapped[uuid.UUID] = uuid_pk()
    idea_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ideas.id"), index=True)
    idea_version: Mapped[int] = mapped_column(Integer, nullable=False)
    repo_id: Mapped[str | None] = mapped_column(String(80))
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dev_task_md: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    created_at: Mapped[datetime] = created_at_col()


class DevelopmentJob(Base):
    __tablename__ = "development_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dev_specs.id"), index=True)
    repo_id: Mapped[str] = mapped_column(String(80), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # awaiting_approval|queued|running|succeeded|failed|rejected|cancelled
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class DevelopmentRun(Base):
    __tablename__ = "development_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("development_jobs.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()


class DevelopmentArtifact(Base):
    __tablename__ = "development_artifacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    spec_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dev_specs.id"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("development_jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # DEV_TASK.md|diff_stat|report
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
