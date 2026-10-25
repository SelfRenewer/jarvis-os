"""spend snapshots (OpenRouter usage history), shopping item source

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01 20:30:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('spend_snapshots',
    sa.Column('taken_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('usage_micro', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=10), nullable=False),
    sa.PrimaryKeyConstraint('taken_at', name=op.f('pk_spend_snapshots'))
    )
    op.add_column('shopping_items', sa.Column('source_ref', sa.String(length=80), nullable=True))
    op.create_index(
        'uq_shopping_items_source_ref', 'shopping_items', ['source_ref'], unique=True,
        postgresql_where='source_ref IS NOT NULL',
    )


def downgrade() -> None:
    op.drop_index('uq_shopping_items_source_ref', table_name='shopping_items', postgresql_where='source_ref IS NOT NULL')
    op.drop_column('shopping_items', 'source_ref')
    op.drop_table('spend_snapshots')
