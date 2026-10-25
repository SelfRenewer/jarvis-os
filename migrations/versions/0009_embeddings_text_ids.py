"""memory_embeddings for search by meaning: text object ids (Todoist), longer model names, (model, type) index

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hashing vectors are lexical and no longer used for search; they are derived data, safe to drop.
    op.execute("DELETE FROM memory_embeddings WHERE model LIKE 'hashing-%'")
    op.alter_column(
        "memory_embeddings",
        "object_id",
        existing_type=sa.UUID(),
        type_=sa.String(length=64),
        existing_nullable=False,
        postgresql_using="object_id::text",
    )
    op.alter_column(
        "memory_embeddings",
        "model",
        existing_type=sa.String(length=60),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
    op.create_index("ix_memory_embeddings_model_type", "memory_embeddings", ["model", "object_type"])


def downgrade() -> None:
    op.drop_index("ix_memory_embeddings_model_type", table_name="memory_embeddings")
    # Vectors of non-UUID objects (Todoist tasks) and long model names cannot go back; they are derived data.
    op.execute(
        "DELETE FROM memory_embeddings WHERE length(model) > 60 OR object_id !~ "
        "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
    )
    op.alter_column(
        "memory_embeddings",
        "model",
        existing_type=sa.String(length=120),
        type_=sa.String(length=60),
        existing_nullable=False,
    )
    op.alter_column(
        "memory_embeddings",
        "object_id",
        existing_type=sa.String(length=64),
        type_=sa.UUID(),
        existing_nullable=False,
        postgresql_using="object_id::uuid",
    )
