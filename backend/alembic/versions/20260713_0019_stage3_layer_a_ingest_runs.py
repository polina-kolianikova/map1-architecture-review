"""Add Stage 3 Layer A ingest run tracking.

Revision ID: 20260713_0019
Revises: 20260708_0018
Create Date: 2026-07-13
"""

import os

from alembic import op


revision = "20260713_0019"
down_revision = "20260708_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage3_layer_a_ingest_runs (
            id BIGSERIAL PRIMARY KEY,
            run_id UUID NOT NULL DEFAULT gen_random_uuid(),
            region_id BIGINT NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            tile_size DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total_tiles INTEGER NOT NULL DEFAULT 0,
            processed_tiles INTEGER NOT NULL DEFAULT 0,
            bolme_fetched INTEGER NOT NULL DEFAULT 0,
            mescere_fetched INTEGER NOT NULL DEFAULT 0,
            bolme_inserted INTEGER NOT NULL DEFAULT 0,
            mescere_inserted INTEGER NOT NULL DEFAULT 0,
            failed_bolme INTEGER NOT NULL DEFAULT 0,
            failed_mescere INTEGER NOT NULL DEFAULT 0,
            bbox JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            CONSTRAINT stage3_layer_a_ingest_runs_status_check
                CHECK (status IN ('running', 'completed', 'failed'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stage3_layer_a_ingest_runs_run_id_uq
        ON stage3_layer_a_ingest_runs (run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_layer_a_ingest_runs_scope_idx
        ON stage3_layer_a_ingest_runs (region_id, started_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage3_layer_a_ingest_tiles (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES stage3_layer_a_ingest_runs(id) ON DELETE CASCADE,
            tile_key TEXT NOT NULL,
            min_lon DOUBLE PRECISION NOT NULL,
            min_lat DOUBLE PRECISION NOT NULL,
            max_lon DOUBLE PRECISION NOT NULL,
            max_lat DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            bolme_fetched INTEGER NOT NULL DEFAULT 0,
            mescere_fetched INTEGER NOT NULL DEFAULT 0,
            bolme_inserted INTEGER NOT NULL DEFAULT 0,
            mescere_inserted INTEGER NOT NULL DEFAULT 0,
            failed_bolme INTEGER NOT NULL DEFAULT 0,
            failed_mescere INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage3_layer_a_ingest_tiles_status_check
                CHECK (status IN ('completed', 'failed'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stage3_layer_a_ingest_tiles_run_tile_uq
        ON stage3_layer_a_ingest_tiles (run_id, tile_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_layer_a_ingest_tiles_status_idx
        ON stage3_layer_a_ingest_tiles (run_id, status, id)
        """
    )
    _grant_runtime_access()


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS stage3_layer_a_ingest_tiles_status_idx")
    op.execute("DROP INDEX IF EXISTS stage3_layer_a_ingest_tiles_run_tile_uq")
    op.execute("DROP TABLE IF EXISTS stage3_layer_a_ingest_tiles")
    op.execute("DROP INDEX IF EXISTS stage3_layer_a_ingest_runs_scope_idx")
    op.execute("DROP INDEX IF EXISTS stage3_layer_a_ingest_runs_run_id_uq")
    op.execute("DROP TABLE IF EXISTS stage3_layer_a_ingest_runs")


def _grant_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE stage3_layer_a_ingest_runs, stage3_layer_a_ingest_tiles TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE stage3_layer_a_ingest_runs_id_seq, stage3_layer_a_ingest_tiles_id_seq TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE stage3_layer_a_ingest_runs, stage3_layer_a_ingest_tiles TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
