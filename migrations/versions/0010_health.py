"""health_payloads, health_samples, health_days: watch and scale data from the phone (Health Connect)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "health_payloads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_health_payloads")),
        sa.UniqueConstraint("sequence", name=op.f("uq_health_payloads_sequence")),
    )
    op.create_index(
        "ix_health_payloads_pending",
        "health_payloads",
        ["received_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.create_table(
        "health_samples",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("record_id", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("value2", sa.Float(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_health_samples")),
        sa.UniqueConstraint("record_id", name=op.f("uq_health_samples_record_id")),
    )
    op.create_index("ix_health_samples_kind_start_at", "health_samples", ["kind", "start_at"])
    op.create_table(
        "health_days",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("active_kcal", sa.Float(), nullable=True),
        sa.Column("total_kcal", sa.Float(), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_health_days")),
    )


def downgrade() -> None:
    op.drop_table("health_days")
    op.drop_index("ix_health_samples_kind_start_at", table_name="health_samples")
    op.drop_table("health_samples")
    op.drop_index("ix_health_payloads_pending", table_name="health_payloads")
    op.drop_table("health_payloads")
