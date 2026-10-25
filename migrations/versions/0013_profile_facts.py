"""profile_facts: what Jarvis knows about the user («запомни, что …»), context for the router and the agent

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-14 22:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_facts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_facts")),
    )
    op.create_index(
        "ix_profile_facts_active", "profile_facts", ["created_at"], postgresql_where=sa.text("status = 'active'")
    )


def downgrade() -> None:
    op.drop_index("ix_profile_facts_active", table_name="profile_facts")
    op.drop_table("profile_facts")
