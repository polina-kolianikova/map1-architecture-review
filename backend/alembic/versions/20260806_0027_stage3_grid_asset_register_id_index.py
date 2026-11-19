"""Add Stage 3 grid asset register id index.

Revision ID: 20260806_0027
Revises: 20260806_0026
Create Date: 2026-08-06 02:27:00.000000
"""

from alembic import op


revision = "20260806_0027"
down_revision = "20260806_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS grid_cells_region_version_id_idx
        ON grid_cells (region_id, grid_version, id) INCLUDE (uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS grid_cells_region_version_id_idx")
