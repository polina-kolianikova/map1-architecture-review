"""Add Stage 5 source-run summary index.

Revision ID: 20260828_0036
Revises: 20260828_0035
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260828_0036"
down_revision: str | None = "20260828_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS carbon_assessments_stage5_source_run_summary_idx
            ON carbon_assessments (
                (source_snapshot->>'source_layer_b_run_id'),
                status,
                coefficient_set_version,
                (metadata->>'coefficient_status'),
                (metadata->>'anomaly_type'),
                (metadata->>'trend_evidence_state')
            )
            INCLUDE (total_potential_tco2e, confidence_score, created_at)
            WHERE is_current
              AND metadata->>'pipeline' = 'stage5_carbon_assessment'
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS carbon_assessments_stage5_source_run_summary_idx")
