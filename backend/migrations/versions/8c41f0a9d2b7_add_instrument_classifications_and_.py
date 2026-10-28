"""add instrument_classifications and directives.allocation_targets

Moves the "what is this instrument" judgement (asset class, region, sector,
size bucket) and the non-asset-class allocation targets out of one-off
scripts and into data, so the allocation engine
(`investment_manager_backend.allocation`) recomputes everything when a
parameter changes instead of requiring a code edit.

Revision ID: 8c41f0a9d2b7
Revises: 375c6255723b
Create Date: 2026-09-21

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '8c41f0a9d2b7'
down_revision: Union[str, None] = '375c6255723b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'instrument_classifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('instrument_name', sa.String(length=300), nullable=False),
        sa.Column('isin', sa.String(length=12), nullable=True),
        sa.Column('asset_class', sa.String(length=20), nullable=False),
        sa.Column('region', sa.String(length=30), nullable=True),
        sa.Column('sector', sa.String(length=30), nullable=True),
        sa.Column('size_bucket', sa.String(length=20), nullable=True),
        sa.Column('confirmed', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'visibility',
            sa.String(length=10),
            server_default='private',
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('instrument_name'),
    )
    op.add_column(
        'directives',
        sa.Column(
            'allocation_targets',
            sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('directives', 'allocation_targets')
    op.drop_table('instrument_classifications')
