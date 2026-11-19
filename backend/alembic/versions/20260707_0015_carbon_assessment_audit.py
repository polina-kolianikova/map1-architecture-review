"""Add carbon assessment audit events.

Revision ID: 20260707_0015
Revises: 20260707_0014
Create Date: 2026-07-07
"""

from alembic import op


revision = "20260707_0015"
down_revision = "20260707_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS carbon_assessment_events (
            id BIGSERIAL PRIMARY KEY,
            event_id UUID NOT NULL DEFAULT gen_random_uuid(),
            carbon_assessment_id BIGINT REFERENCES carbon_assessments(id) ON DELETE SET NULL,
            grid_cell_id BIGINT REFERENCES grid_cells(id) ON DELETE SET NULL,
            verification_result_id BIGINT REFERENCES verification_results(id) ON DELETE SET NULL,
            observation_id BIGINT REFERENCES sentinel_observations(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT NOT NULL,
            coefficient_set_version TEXT,
            config_hash TEXT,
            actor_type TEXT NOT NULL DEFAULT 'system',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT carbon_assessment_events_type_check
                CHECK (event_type IN ('created', 'status_changed', 'status_recalculated', 'skipped'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS carbon_assessment_events_event_id_uq
        ON carbon_assessment_events (event_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_cell_created_idx
        ON carbon_assessment_events (grid_cell_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_config_hash_idx
        ON carbon_assessment_events (config_hash)
        WHERE config_hash IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_metadata_gin
        ON carbon_assessment_events USING GIN (metadata)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_carbon_assessment_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'carbon_assessment_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER carbon_assessment_events_no_update_delete
        BEFORE UPDATE OR DELETE ON carbon_assessment_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_carbon_assessment_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER carbon_assessment_events_no_truncate
        BEFORE TRUNCATE ON carbon_assessment_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION prevent_carbon_assessment_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS carbon_assessment_events_no_truncate ON carbon_assessment_events")
    op.execute("DROP TRIGGER IF EXISTS carbon_assessment_events_no_update_delete ON carbon_assessment_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_carbon_assessment_event_mutation()")
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_metadata_gin")
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_config_hash_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_cell_created_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_event_id_uq")
    op.execute("DROP TABLE IF EXISTS carbon_assessment_events")
