"""Add Stage 3 scope expression indexes.

Revision ID: 20260707_0016
Revises: 20260707_0015
Create Date: 2026-07-07
"""

from alembic import op


revision = "20260707_0016"
down_revision = "20260707_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ogm_parcels_current_region_layer_idx
        ON ogm_parcels ((attributes->>'region_code'), source_layer, fetched_at DESC)
        WHERE is_current
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_stage_region_grid_cell_date_idx
        ON sentinel_observations (
            (metadata->>'region_code'),
            (metadata->>'grid_version'),
            grid_cell_id,
            acquisition_date DESC,
            id DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_results_stage_region_grid_cell_idx
        ON verification_results (
            (metadata->>'region_code'),
            (metadata->>'grid_version'),
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
        CREATE INDEX IF NOT EXISTS verification_audit_events_stage_region_grid_cell_idx
        ON verification_audit_events (
            (metadata->>'region_code'),
            (metadata->'latest_result_metadata'->>'grid_version'),
            grid_cell_id,
            created_at DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_verification_audit'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_audit_events_stage_region_grid_cell_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_stage_region_grid_cell_idx")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_stage_region_grid_cell_date_idx")
    op.execute("DROP INDEX IF EXISTS ogm_parcels_current_region_layer_idx")
