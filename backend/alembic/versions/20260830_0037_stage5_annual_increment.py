"""Add Stage 5 annual increment fields.

Revision ID: 20260830_0037
Revises: 20260828_0036
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260830_0037"
down_revision: str | None = "20260828_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE carbon_assessments
            ADD COLUMN IF NOT EXISTS assessment_year INTEGER,
            ADD COLUMN IF NOT EXISTS previous_annual_assessment_id BIGINT,
            ADD COLUMN IF NOT EXISTS gross_increment_tco2e_ha_yr NUMERIC(14, 4),
            ADD COLUMN IF NOT EXISTS loss_factor_tco2e_ha_yr NUMERIC(14, 4),
            ADD COLUMN IF NOT EXISTS net_annual_increment_tco2e_ha_yr NUMERIC(14, 4),
            ADD COLUMN IF NOT EXISTS net_annual_increment_tco2e_yr NUMERIC(14, 4),
            ADD COLUMN IF NOT EXISTS total_potential_delta_tco2e NUMERIC(14, 4),
            ADD COLUMN IF NOT EXISTS net_annual_increment_delta_tco2e_ha_yr NUMERIC(14, 4),
            ADD COLUMN IF NOT EXISTS net_annual_increment_delta_tco2e_yr NUMERIC(14, 4)
        """
    )
    op.execute(
        """
        ALTER TABLE carbon_assessments
            DROP CONSTRAINT IF EXISTS carbon_assessments_assessment_year_valid,
            ADD CONSTRAINT carbon_assessments_assessment_year_valid
                CHECK (assessment_year IS NULL OR assessment_year BETWEEN 1900 AND 2200),
            DROP CONSTRAINT IF EXISTS carbon_assessments_annual_increment_inputs_non_negative,
            ADD CONSTRAINT carbon_assessments_annual_increment_inputs_non_negative
                CHECK (
                    (gross_increment_tco2e_ha_yr IS NULL OR gross_increment_tco2e_ha_yr >= 0)
                    AND (loss_factor_tco2e_ha_yr IS NULL OR loss_factor_tco2e_ha_yr >= 0)
                )
        """
    )
    op.execute(
        """
        ALTER TABLE stage5_carbon_summaries
            ADD COLUMN IF NOT EXISTS net_annual_increment_tco2e_yr NUMERIC,
            ADD COLUMN IF NOT EXISTS average_net_annual_increment_tco2e_ha_yr NUMERIC
        """
    )
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS carbon_assessments_stage5_yoy_lookup_idx
            ON carbon_assessments (
                grid_cell_id,
                assessment_year DESC,
                created_at DESC,
                id DESC
            )
            INCLUDE (
                total_potential_tco2e,
                net_annual_increment_tco2e_ha_yr,
                net_annual_increment_tco2e_yr
            )
            WHERE metadata->>'pipeline' = 'stage5_carbon_assessment'
              AND assessment_year IS NOT NULL
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS carbon_assessments_stage5_increment_summary_idx
            ON carbon_assessments (
                (source_snapshot->>'source_layer_b_run_id'),
                status,
                (metadata->>'increment_model')
            )
            INCLUDE (
                gross_increment_tco2e_ha_yr,
                loss_factor_tco2e_ha_yr,
                net_annual_increment_tco2e_ha_yr,
                net_annual_increment_tco2e_yr,
                created_at
            )
            WHERE is_current
              AND metadata->>'pipeline' = 'stage5_carbon_assessment'
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS carbon_assessments_stage5_increment_summary_idx")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS carbon_assessments_stage5_yoy_lookup_idx")
    op.execute(
        """
        ALTER TABLE stage5_carbon_summaries
            DROP COLUMN IF EXISTS average_net_annual_increment_tco2e_ha_yr,
            DROP COLUMN IF EXISTS net_annual_increment_tco2e_yr
        """
    )
    op.execute(
        """
        ALTER TABLE carbon_assessments
            DROP CONSTRAINT IF EXISTS carbon_assessments_assessment_year_valid,
            DROP CONSTRAINT IF EXISTS carbon_assessments_annual_increment_inputs_non_negative,
            DROP COLUMN IF EXISTS net_annual_increment_delta_tco2e_yr,
            DROP COLUMN IF EXISTS net_annual_increment_delta_tco2e_ha_yr,
            DROP COLUMN IF EXISTS total_potential_delta_tco2e,
            DROP COLUMN IF EXISTS net_annual_increment_tco2e_yr,
            DROP COLUMN IF EXISTS net_annual_increment_tco2e_ha_yr,
            DROP COLUMN IF EXISTS loss_factor_tco2e_ha_yr,
            DROP COLUMN IF EXISTS gross_increment_tco2e_ha_yr,
            DROP COLUMN IF EXISTS previous_annual_assessment_id,
            DROP COLUMN IF EXISTS assessment_year
        """
    )
