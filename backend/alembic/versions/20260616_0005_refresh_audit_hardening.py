"""Harden refresh and audit history.

Revision ID: 20260616_0005
Revises: 20260610_0004
Create Date: 2026-06-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260616_0005"
down_revision: str | None = "20260610_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE verification_audit_events
            DROP CONSTRAINT IF EXISTS verification_audit_events_verification_result_id_fkey,
            DROP CONSTRAINT IF EXISTS verification_audit_events_grid_cell_id_fkey,
            DROP CONSTRAINT IF EXISTS verification_audit_events_ogm_parcel_id_fkey,
            DROP CONSTRAINT IF EXISTS verification_audit_events_observation_id_fkey
        """
    )

    op.execute(
        """
        ALTER TABLE ogm_parcels
            ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT true,
            ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )
    op.execute(
        """
        ALTER TABLE sentinel_observations
            ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT true,
            ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ogm_parcels_current_source_idx
        ON ogm_parcels (source_layer, source_id)
        WHERE is_current
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ogm_parcels_current_geom_gix
        ON ogm_parcels USING GIST (geom)
        WHERE is_current
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_current_grid_date_idx
        ON sentinel_observations (grid_cell_id, acquisition_date DESC, id DESC)
        WHERE is_current
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS sentinel_observations_current_grid_date_idx")
    op.execute("DROP INDEX IF EXISTS ogm_parcels_current_geom_gix")
    op.execute("DROP INDEX IF EXISTS ogm_parcels_current_source_idx")
    op.execute("ALTER TABLE sentinel_observations DROP COLUMN IF EXISTS last_seen_at")
    op.execute("ALTER TABLE sentinel_observations DROP COLUMN IF EXISTS is_current")
    op.execute("ALTER TABLE ogm_parcels DROP COLUMN IF EXISTS last_seen_at")
    op.execute("ALTER TABLE ogm_parcels DROP COLUMN IF EXISTS is_current")

    op.execute(
        """
        ALTER TABLE verification_audit_events
            ADD CONSTRAINT verification_audit_events_verification_result_id_fkey
                FOREIGN KEY (verification_result_id)
                REFERENCES verification_results(id)
                ON DELETE SET NULL
                NOT VALID,
            ADD CONSTRAINT verification_audit_events_grid_cell_id_fkey
                FOREIGN KEY (grid_cell_id)
                REFERENCES grid_cells(id)
                ON DELETE SET NULL
                NOT VALID,
            ADD CONSTRAINT verification_audit_events_ogm_parcel_id_fkey
                FOREIGN KEY (ogm_parcel_id)
                REFERENCES ogm_parcels(id)
                ON DELETE SET NULL
                NOT VALID,
            ADD CONSTRAINT verification_audit_events_observation_id_fkey
                FOREIGN KEY (observation_id)
                REFERENCES sentinel_observations(id)
                ON DELETE SET NULL
                NOT VALID
        """
    )
