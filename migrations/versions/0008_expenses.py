"""expenses (finance agent: «потратил 45 лари на такси», receipt photos)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 01:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expenses",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("spent_on", sa.Date(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("merchant", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_ref", sa.String(length=100), nullable=True),
        sa.Column("raw_media_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["raw_media_id"], ["raw_media.id"], name=op.f("fk_expenses_raw_media_id_raw_media")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expenses")),
        sa.UniqueConstraint("source_ref", name=op.f("uq_expenses_source_ref")),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_expenses_positive_amount")),
    )
    op.create_index(
        "ix_expenses_spent_on", "expenses", ["spent_on"], unique=False, postgresql_where="deleted_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_spent_on", table_name="expenses", postgresql_where="deleted_at IS NULL")
    op.drop_table("expenses")
