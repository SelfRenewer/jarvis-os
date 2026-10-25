"""mac actions (Jarvis for Mac)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 21:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('mac_actions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=True),
    sa.Column('detail', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mac_actions'))
    )
    op.create_index('ix_mac_actions_pending', 'mac_actions', ['created_at'], unique=False, postgresql_where="status = 'pending'")


def downgrade() -> None:
    op.drop_index('ix_mac_actions_pending', table_name='mac_actions', postgresql_where="status = 'pending'")
    op.drop_table('mac_actions')
