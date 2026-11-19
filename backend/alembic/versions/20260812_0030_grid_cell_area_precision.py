"""Increase clipped grid cell area precision.

Revision ID: 20260812_0030
Revises: 20260806_0029
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260812_0030"
down_revision: str | None = "20260806_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE grid_cells
        ALTER COLUMN area_ha TYPE NUMERIC(14, 6)
        USING area_ha::numeric(14, 6)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE grid_cells
        ALTER COLUMN area_ha TYPE NUMERIC(10, 4)
        USING area_ha::numeric(10, 4)
        """
    )
