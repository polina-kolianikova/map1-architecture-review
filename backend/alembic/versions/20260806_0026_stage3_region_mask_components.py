"""stage3 region mask components

Revision ID: 20260806_0026
Revises: 20260801_0025
Create Date: 2026-08-06 00:26:00.000000
"""

import os

from alembic import op


revision = "20260806_0026"
down_revision = "20260801_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage3_region_mask_components (
            id BIGSERIAL PRIMARY KEY,
            region_id BIGINT NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
            mask_version TEXT NOT NULL,
            component_index INTEGER NOT NULL,
            projected_srid INTEGER NOT NULL,
            geom_4326 geometry(Geometry, 4326) NOT NULL,
            geom_projected geometry(Geometry) NOT NULL,
            area_ha NUMERIC(14, 4) NOT NULL DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (region_id, mask_version, component_index)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_region_mask_components_scope_idx
        ON stage3_region_mask_components (region_id, mask_version, projected_srid)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_region_mask_components_projected_gix
        ON stage3_region_mask_components
        USING GIST (geom_projected)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage3_region_mask_components_4326_gix
        ON stage3_region_mask_components
        USING GIST (geom_4326)
        """
    )
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE stage3_region_mask_components TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE stage3_region_mask_components_id_seq TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE stage3_region_mask_components TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS stage3_region_mask_components_4326_gix")
    op.execute("DROP INDEX IF EXISTS stage3_region_mask_components_projected_gix")
    op.execute("DROP INDEX IF EXISTS stage3_region_mask_components_scope_idx")
    op.execute("DROP TABLE IF EXISTS stage3_region_mask_components")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
