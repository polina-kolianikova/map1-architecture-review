"""Add Stage 3 asset query indexes.

Revision ID: 20260707_0013
Revises: 20260707_0012
Create Date: 2026-07-07
"""

from alembic import op


revision = "20260707_0013"
down_revision = "20260707_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS grid_cells_region_version_uuid_idx
        ON grid_cells (region_id, grid_version, uuid)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_stage_scope_date_idx
        ON sentinel_observations (
            grid_cell_id,
            acquisition_date DESC,
            id DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_stage_status_idx
        ON sentinel_observations (
            (COALESCE(metadata->>'status', 'ok')),
            acquisition_date DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_results_stage_cell_current_idx
        ON verification_results (
            grid_cell_id,
            created_at DESC,
            id DESC
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_current_status_cell_idx
        ON carbon_assessments (
            status,
            grid_cell_id,
            created_at DESC
        )
        WHERE is_current
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS carbon_assessments_current_status_cell_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_stage_cell_current_idx")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_stage_status_idx")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_stage_scope_date_idx")
    op.execute("DROP INDEX IF EXISTS grid_cells_region_version_uuid_idx")
