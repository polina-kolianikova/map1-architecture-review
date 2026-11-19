"""Add Stage 3 grid generation run tracking.

Revision ID: 20260707_0017
Revises: 20260707_0016
Create Date: 2026-07-07
"""

import os

from alembic import op


revision = "20260707_0017"
down_revision = "20260707_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage3_grid_generation_runs (
            id BIGSERIAL PRIMARY KEY,
            run_id UUID NOT NULL DEFAULT gen_random_uuid(),
            region_id BIGINT NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
            grid_version TEXT NOT NULL,
            grid_size_m INTEGER NOT NULL,
            projected_srid INTEGER NOT NULL,
            tile_size_m INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total_tiles INTEGER NOT NULL DEFAULT 0,
            processed_tiles INTEGER NOT NULL DEFAULT 0,
            inserted_cells BIGINT NOT NULL DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            CONSTRAINT stage3_grid_generation_runs_status_check
                CHECK (status IN ('running', 'completed', 'failed'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stage3_grid_generation_runs_run_id_uq
        ON stage3_grid_generation_runs (run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_grid_generation_runs_scope_idx
        ON stage3_grid_generation_runs (region_id, grid_version, started_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage3_grid_generation_tiles (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES stage3_grid_generation_runs(id) ON DELETE CASCADE,
            tile_key TEXT NOT NULL,
            min_x DOUBLE PRECISION NOT NULL,
            min_y DOUBLE PRECISION NOT NULL,
            max_x DOUBLE PRECISION NOT NULL,
            max_y DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            inserted_cells INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            processed_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT stage3_grid_generation_tiles_status_check
                CHECK (status IN ('pending', 'completed', 'failed'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stage3_grid_generation_tiles_run_tile_uq
        ON stage3_grid_generation_tiles (run_id, tile_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_grid_generation_tiles_status_idx
        ON stage3_grid_generation_tiles (run_id, status, id)
        """
    )
    _grant_runtime_access()


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS stage3_grid_generation_tiles_status_idx")
    op.execute("DROP INDEX IF EXISTS stage3_grid_generation_tiles_run_tile_uq")
    op.execute("DROP TABLE IF EXISTS stage3_grid_generation_tiles")
    op.execute("DROP INDEX IF EXISTS stage3_grid_generation_runs_scope_idx")
    op.execute("DROP INDEX IF EXISTS stage3_grid_generation_runs_run_id_uq")
    op.execute("DROP TABLE IF EXISTS stage3_grid_generation_runs")


def _grant_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE stage3_grid_generation_runs TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE stage3_grid_generation_tiles TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE stage3_grid_generation_runs_id_seq, stage3_grid_generation_tiles_id_seq TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE stage3_grid_generation_runs, stage3_grid_generation_tiles TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
