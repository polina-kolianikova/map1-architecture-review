"""Protect current Sentinel observations from duplicate refreshes.

Revision ID: 20260616_0008
Revises: 20260616_0007
Create Date: 2026-06-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260616_0008"
down_revision: str | None = "20260616_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY
                        grid_cell_id,
                        metadata->>'pipeline',
                        metadata->>'region_code',
                        metadata->>'grid_version'
                    ORDER BY
                        acquisition_date DESC,
                        last_seen_at DESC NULLS LAST,
                        created_at DESC,
                        id DESC
                ) AS row_number
            FROM sentinel_observations
            WHERE is_current
              AND metadata->>'pipeline' = 'stage1_cdse_ndvi'
              AND metadata->>'region_code' IS NOT NULL
              AND metadata->>'grid_version' IS NOT NULL
        )
        UPDATE sentinel_observations observation
        SET is_current = false
        FROM ranked
        WHERE ranked.id = observation.id
          AND ranked.row_number > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS sentinel_observations_current_stage1_cell_uq
        ON sentinel_observations (
            grid_cell_id,
            (metadata->>'pipeline'),
            (metadata->>'region_code'),
            (metadata->>'grid_version')
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_cdse_ndvi'
          AND metadata->>'region_code' IS NOT NULL
          AND metadata->>'grid_version' IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS sentinel_observations_current_stage1_cell_uq")
