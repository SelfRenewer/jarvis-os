"""screen_days: phone screen time (Life Dashboard «Screen Time»); medications.ends_on: courses with an end

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-14 17:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screen_days",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("apps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_screen_days")),
    )
    op.add_column("medications", sa.Column("ends_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("medications", "ends_on")
    op.drop_table("screen_days")
