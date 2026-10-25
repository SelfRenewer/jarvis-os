"""medication packs (several supplements in one reminder) and partial intakes

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13 23:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "medications",
        sa.Column(
            "items", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.add_column(
        "medication_intakes", sa.Column("items_taken", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("medication_intakes", "items_taken")
    op.drop_column("medications", "items")
