"""Add Stage 3 scope columns for scale queries.

Revision ID: 20260726_0022
Revises: 20260724_0021
Create Date: 2026-07-26
"""

from collections.abc import Sequence
import os

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0022"
down_revision: str | None = "20260724_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCOPE_TABLES = (
    "sentinel_observations",
    "verification_results",
    "verification_audit_events",
)


def upgrade() -> None:
    for table_name in SCOPE_TABLES:
        op.add_column(table_name, sa.Column("region_id", sa.BigInteger(), nullable=True))
        op.add_column(table_name, sa.Column("region_code", sa.Text(), nullable=True))
        op.add_column(table_name, sa.Column("grid_version", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE sentinel_observations observation
        SET region_id = region.id,
            region_code = region.code,
            grid_version = COALESCE(observation.metadata->>'grid_version', cell.grid_version)
        FROM grid_cells cell
        JOIN regions region ON region.id = cell.region_id
        WHERE observation.grid_cell_id = cell.id
          AND observation.region_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE verification_results result
        SET region_id = region.id,
            region_code = region.code,
            grid_version = COALESCE(result.metadata->>'grid_version', cell.grid_version)
        FROM grid_cells cell
        JOIN regions region ON region.id = cell.region_id
        WHERE result.grid_cell_id = cell.id
          AND result.region_id IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE verification_audit_events
        DISABLE TRIGGER verification_audit_events_no_update_delete
        """
    )
    op.execute(
        """
        UPDATE verification_audit_events event
        SET region_id = region.id,
            region_code = region.code,
            grid_version = COALESCE(
                event.metadata->>'grid_version',
                event.metadata->'latest_result_metadata'->>'grid_version',
                cell.grid_version
            )
        FROM grid_cells cell
        JOIN regions region ON region.id = cell.region_id
        WHERE event.grid_cell_id = cell.id
          AND event.region_id IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE verification_audit_events
        ENABLE TRIGGER verification_audit_events_no_update_delete
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_scope_grid_cell_date_idx
        ON sentinel_observations (region_id, grid_version, grid_cell_id, acquisition_date DESC, id DESC)
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_results_scope_grid_cell_idx
        ON verification_results (region_id, grid_version, grid_cell_id, created_at DESC, id DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_results_scope_status_idx
        ON verification_results (region_id, grid_version, anomaly_type, created_at DESC, id DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_audit_events_scope_cell_created_idx
        ON verification_audit_events (region_id, grid_version, grid_cell_id, created_at DESC)
        WHERE metadata->>'pipeline' = 'stage1_verification_audit'
        """
    )

    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE sentinel_observations, verification_results, verification_audit_events TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT INSERT (region_id, region_code, grid_version) ON TABLE sentinel_observations, verification_results, verification_audit_events TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT UPDATE (region_id, region_code, grid_version) ON TABLE sentinel_observations, verification_results, verification_audit_events TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE sentinel_observations, verification_results, verification_audit_events TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_audit_events_scope_cell_created_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_scope_status_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_scope_grid_cell_idx")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_scope_grid_cell_date_idx")
    for table_name in reversed(SCOPE_TABLES):
        op.drop_column(table_name, "grid_version")
        op.drop_column(table_name, "region_code")
        op.drop_column(table_name, "region_id")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
