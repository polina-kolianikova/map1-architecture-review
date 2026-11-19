"""Add carbon assessment provenance.

Revision ID: 20260707_0014
Revises: 20260707_0013
Create Date: 2026-07-07
"""

from alembic import op


revision = "20260707_0014"
down_revision = "20260707_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE carbon_assessments
            ADD COLUMN IF NOT EXISTS verification_result_id BIGINT REFERENCES verification_results(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS observation_id BIGINT REFERENCES sentinel_observations(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS ogm_parcel_id BIGINT REFERENCES ogm_parcels(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS config_hash TEXT,
            ADD COLUMN IF NOT EXISTS source_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'carbon_assessments_status_workflow'
            ) THEN
                ALTER TABLE carbon_assessments
                    ADD CONSTRAINT carbon_assessments_status_workflow
                    CHECK (
                        status IN (
                            'pending_inputs',
                            'pending_coefficients',
                            'calculated',
                            'skipped_missing_evidence',
                            'failed'
                        )
                    );
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_config_hash_idx
        ON carbon_assessments (config_hash)
        WHERE config_hash IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_provenance_idx
        ON carbon_assessments (verification_result_id, observation_id, ogm_parcel_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS carbon_assessments_provenance_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_config_hash_idx")
    op.execute("ALTER TABLE carbon_assessments DROP CONSTRAINT IF EXISTS carbon_assessments_status_workflow")
    op.execute("ALTER TABLE carbon_assessments DROP COLUMN IF EXISTS source_snapshot")
    op.execute("ALTER TABLE carbon_assessments DROP COLUMN IF EXISTS config_hash")
    op.execute("ALTER TABLE carbon_assessments DROP COLUMN IF EXISTS ogm_parcel_id")
    op.execute("ALTER TABLE carbon_assessments DROP COLUMN IF EXISTS observation_id")
    op.execute("ALTER TABLE carbon_assessments DROP COLUMN IF EXISTS verification_result_id")
