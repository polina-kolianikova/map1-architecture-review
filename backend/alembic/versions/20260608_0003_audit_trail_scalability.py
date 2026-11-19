"""Add verification audit trail and scalability indexes.

Revision ID: 20260608_0003
Revises: 20260528_0002
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260608_0003"
down_revision: str | None = "20260528_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE verification_audit_events (
            id BIGSERIAL PRIMARY KEY,
            event_id UUID NOT NULL DEFAULT gen_random_uuid(),
            verification_result_id BIGINT REFERENCES verification_results(id) ON DELETE SET NULL,
            grid_cell_id BIGINT REFERENCES grid_cells(id) ON DELETE SET NULL,
            ogm_parcel_id BIGINT REFERENCES ogm_parcels(id) ON DELETE SET NULL,
            observation_id BIGINT REFERENCES sentinel_observations(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT NOT NULL,
            previous_score NUMERIC(8, 5),
            new_score NUMERIC(8, 5) NOT NULL,
            reason TEXT NOT NULL,
            source_data_version TEXT NOT NULL,
            actor_type TEXT NOT NULL DEFAULT 'system',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT verification_audit_events_score_range CHECK (
                (previous_score IS NULL OR previous_score BETWEEN 0 AND 1)
                AND new_score BETWEEN 0 AND 1
            ),
            CONSTRAINT verification_audit_events_type_check CHECK (
                event_type IN ('created', 'status_changed', 'status_recalculated')
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX verification_audit_events_event_id_uq "
        "ON verification_audit_events (event_id)"
    )
    op.execute(
        "CREATE INDEX verification_audit_events_result_id_idx "
        "ON verification_audit_events (verification_result_id)"
    )
    op.execute(
        "CREATE INDEX verification_audit_events_grid_cell_created_idx "
        "ON verification_audit_events (grid_cell_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX verification_audit_events_event_type_created_idx "
        "ON verification_audit_events (event_type, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX verification_audit_events_source_data_version_idx "
        "ON verification_audit_events (source_data_version)"
    )
    op.execute(
        "CREATE INDEX verification_audit_events_metadata_gin "
        "ON verification_audit_events USING GIN (metadata)"
    )

    op.execute(
        "CREATE INDEX grid_cells_region_version_idx "
        "ON grid_cells (region_id, grid_version)"
    )
    op.execute(
        "CREATE INDEX ogm_parcels_source_layer_fetched_idx "
        "ON ogm_parcels (source_layer, fetched_at DESC)"
    )
    op.execute("CREATE INDEX ogm_parcels_attributes_gin ON ogm_parcels USING GIN (attributes)")
    op.execute(
        "CREATE INDEX sentinel_observations_grid_date_idx "
        "ON sentinel_observations (grid_cell_id, acquisition_date DESC)"
    )
    op.execute(
        "CREATE INDEX verification_results_cell_created_idx "
        "ON verification_results (grid_cell_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX verification_results_status_created_idx "
        "ON verification_results (anomaly_type, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_results_status_created_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_cell_created_idx")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_grid_date_idx")
    op.execute("DROP INDEX IF EXISTS ogm_parcels_attributes_gin")
    op.execute("DROP INDEX IF EXISTS ogm_parcels_source_layer_fetched_idx")
    op.execute("DROP INDEX IF EXISTS grid_cells_region_version_idx")
    op.execute("DROP TABLE IF EXISTS verification_audit_events")
