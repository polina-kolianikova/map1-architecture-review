"""Add Stage 3 carbon read indexes.

Revision ID: 20260806_0028
Revises: 20260806_0027
Create Date: 2026-08-06 16:35:00.000000
"""

from alembic import op


revision = "20260806_0028"
down_revision = "20260806_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_current_created_cell_idx
        ON carbon_assessments (created_at DESC, id DESC, grid_cell_id)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage3_carbon_assessment'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_pipeline_created_cell_idx
        ON carbon_assessment_events (created_at DESC, id DESC, grid_cell_id)
        WHERE metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_pipeline_created_cell_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_current_created_cell_idx")
