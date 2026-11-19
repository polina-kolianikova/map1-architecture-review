"""Decouple carbon audit events from mutable source rows.

Revision ID: 20260724_0021
Revises: 20260721_0020
Create Date: 2026-07-24
"""

from alembic import op


revision = "20260724_0021"
down_revision = "20260721_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for constraint_name in (
        "carbon_assessment_events_carbon_assessment_id_fkey",
        "carbon_assessment_events_grid_cell_id_fkey",
        "carbon_assessment_events_verification_result_id_fkey",
        "carbon_assessment_events_observation_id_fkey",
    ):
        op.execute(f"ALTER TABLE carbon_assessment_events DROP CONSTRAINT IF EXISTS {constraint_name}")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE carbon_assessment_events
            ADD CONSTRAINT carbon_assessment_events_carbon_assessment_id_fkey
            FOREIGN KEY (carbon_assessment_id)
            REFERENCES carbon_assessments(id)
            ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE carbon_assessment_events
            ADD CONSTRAINT carbon_assessment_events_grid_cell_id_fkey
            FOREIGN KEY (grid_cell_id)
            REFERENCES grid_cells(id)
            ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE carbon_assessment_events
            ADD CONSTRAINT carbon_assessment_events_verification_result_id_fkey
            FOREIGN KEY (verification_result_id)
            REFERENCES verification_results(id)
            ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE carbon_assessment_events
            ADD CONSTRAINT carbon_assessment_events_observation_id_fkey
            FOREIGN KEY (observation_id)
            REFERENCES sentinel_observations(id)
            ON DELETE SET NULL
        """
    )
