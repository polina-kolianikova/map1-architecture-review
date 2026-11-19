"""Repair duplicate current verification results.

Revision ID: 20260616_0009
Revises: 20260616_0008
Create Date: 2026-06-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260616_0009"
down_revision: str | None = "20260616_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_results_current_stage1_cell_uq")
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY grid_cell_id
                    ORDER BY created_at DESC, id DESC
                ) AS row_number
            FROM verification_results
            WHERE is_current
              AND metadata->>'pipeline' = 'stage1_verification'
        )
        UPDATE verification_results result
        SET is_current = false
        FROM ranked
        WHERE ranked.id = result.id
          AND ranked.row_number > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS verification_results_current_stage1_cell_uq
        ON verification_results (grid_cell_id)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_results_current_stage1_cell_uq")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS verification_results_current_stage1_cell_uq
        ON verification_results (grid_cell_id)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )
