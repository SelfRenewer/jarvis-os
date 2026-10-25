"""call_requests.spoken_text: Telegram calls are queued for the `caller` service with what to say

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-14 19:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("call_requests", sa.Column("spoken_text", sa.Text(), nullable=True))
    op.create_index(
        "ix_call_requests_telegram_queued",
        "call_requests",
        ["requested_for"],
        postgresql_where=sa.text("provider = 'telegram' AND status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("ix_call_requests_telegram_queued", table_name="call_requests")
    op.drop_column("call_requests", "spoken_text")
