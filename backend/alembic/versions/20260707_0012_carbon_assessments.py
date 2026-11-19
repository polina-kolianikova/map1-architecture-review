"""Add carbon assessment records.

Revision ID: 20260707_0012
Revises: 20260619_0011
Create Date: 2026-07-07
"""

from alembic import op


revision = "20260707_0012"
down_revision = "20260619_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS carbon_assessments (
            id BIGSERIAL PRIMARY KEY,
            grid_cell_id BIGINT NOT NULL REFERENCES grid_cells(id) ON DELETE CASCADE,
            methodology_version TEXT NOT NULL,
            coefficient_set_version TEXT,
            soil_tco2e NUMERIC(14, 4),
            biomass_tco2e NUMERIC(14, 4),
            root_tco2e NUMERIC(14, 4),
            total_potential_tco2e NUMERIC(14, 4),
            verification_factor NUMERIC(8, 5),
            confidence_score NUMERIC(5, 2),
            calibration_source TEXT,
            status TEXT NOT NULL DEFAULT 'pending_inputs',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_current BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT carbon_assessments_confidence_score_range
                CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 100),
            CONSTRAINT carbon_assessments_verification_factor_positive
                CHECK (verification_factor IS NULL OR verification_factor >= 0),
            CONSTRAINT carbon_assessments_component_values_positive
                CHECK (
                    (soil_tco2e IS NULL OR soil_tco2e >= 0)
                    AND (biomass_tco2e IS NULL OR biomass_tco2e >= 0)
                    AND (root_tco2e IS NULL OR root_tco2e >= 0)
                    AND (total_potential_tco2e IS NULL OR total_potential_tco2e >= 0)
                ),
            CONSTRAINT carbon_assessments_status_not_blank
                CHECK (length(trim(status)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS carbon_assessments_current_cell_uq
        ON carbon_assessments (grid_cell_id)
        WHERE is_current
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_cell_created_idx
        ON carbon_assessments (grid_cell_id, created_at DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_status_idx
        ON carbon_assessments (status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_metadata_gin
        ON carbon_assessments USING GIN (metadata)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS carbon_assessments_metadata_gin")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_status_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_cell_created_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_current_cell_uq")
    op.execute("DROP TABLE IF EXISTS carbon_assessments")
