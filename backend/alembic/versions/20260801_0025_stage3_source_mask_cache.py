"""Add Stage 3 source mask cache.

Revision ID: 20260801_0025
Revises: 20260729_0024
Create Date: 2026-08-01
"""

from collections.abc import Sequence
import os

from alembic import op


revision: str = "20260801_0025"
down_revision: str | None = "20260729_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage3_source_mask_cache (
            id BIGSERIAL PRIMARY KEY,
            source_region_code TEXT NOT NULL,
            source_layer TEXT NOT NULL,
            projected_srid INTEGER NOT NULL,
            source_parcel_id BIGINT NOT NULL REFERENCES ogm_parcels(id) ON DELETE CASCADE,
            component_index INTEGER NOT NULL,
            geom_4326 geometry(Geometry, 4326) NOT NULL,
            geom_projected geometry(Geometry) NOT NULL,
            geom_hash TEXT NOT NULL,
            area_ha NUMERIC(14, 4) NOT NULL DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_source_mask_cache_scope_idx
        ON stage3_source_mask_cache (
            source_region_code,
            projected_srid,
            source_layer
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_source_mask_cache_projected_gix
        ON stage3_source_mask_cache
        USING GIST (geom_projected)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_source_mask_cache_4326_gix
        ON stage3_source_mask_cache
        USING GIST (geom_4326)
        """
    )
    _grant_runtime_access()


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS stage3_source_mask_cache_4326_gix")
    op.execute("DROP INDEX IF EXISTS stage3_source_mask_cache_projected_gix")
    op.execute("DROP INDEX IF EXISTS stage3_source_mask_cache_scope_idx")
    op.execute("DROP TABLE IF EXISTS stage3_source_mask_cache")


def _grant_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE stage3_source_mask_cache TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE stage3_source_mask_cache_id_seq TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE stage3_source_mask_cache TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
