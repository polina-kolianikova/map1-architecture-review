"""Initial PostGIS schema.

Revision ID: 20260527_0001
Revises:
Create Date: 2026-05-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260527_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE regions (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            country_code CHAR(2) NOT NULL DEFAULT 'TR',
            geom geometry(MultiPolygon, 4326) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT regions_geom_srid CHECK (ST_SRID(geom) = 4326)
        )
        """
    )
    op.execute("CREATE INDEX regions_geom_gix ON regions USING GIST (geom)")
    op.execute("CREATE INDEX regions_country_code_idx ON regions (country_code)")

    op.execute(
        """
        CREATE TABLE grid_cells (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL DEFAULT gen_random_uuid(),
            region_id BIGINT REFERENCES regions(id) ON DELETE SET NULL,
            geom geometry(Polygon, 4326) NOT NULL,
            area_ha NUMERIC(10, 4) NOT NULL,
            grid_version TEXT NOT NULL DEFAULT 'stage1-test',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT grid_cells_uuid_grid_version_uq UNIQUE (uuid, grid_version),
            CONSTRAINT grid_cells_area_positive CHECK (area_ha > 0),
            CONSTRAINT grid_cells_geom_srid CHECK (ST_SRID(geom) = 4326)
        )
        """
    )
    op.execute("CREATE INDEX grid_cells_geom_gix ON grid_cells USING GIST (geom)")
    op.execute("CREATE INDEX grid_cells_region_id_idx ON grid_cells (region_id)")
    op.execute("CREATE INDEX grid_cells_uuid_idx ON grid_cells (uuid)")
    op.execute("CREATE INDEX grid_cells_grid_version_idx ON grid_cells (grid_version)")

    op.execute(
        """
        CREATE TABLE ogm_parcels (
            id BIGSERIAL PRIMARY KEY,
            source_id TEXT,
            source_layer TEXT NOT NULL,
            forest_type TEXT,
            tree_age INTEGER,
            official_status TEXT,
            attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
            geom geometry(Geometry, 4326) NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            source_updated_at TIMESTAMPTZ,
            CONSTRAINT ogm_parcels_geom_srid CHECK (ST_SRID(geom) = 4326)
        )
        """
    )
    op.execute("CREATE INDEX ogm_parcels_geom_gix ON ogm_parcels USING GIST (geom)")
    op.execute("CREATE INDEX ogm_parcels_source_idx ON ogm_parcels (source_layer, source_id)")
    op.execute("CREATE INDEX ogm_parcels_status_idx ON ogm_parcels (official_status)")

    op.execute(
        """
        CREATE TABLE sentinel_observations (
            id BIGSERIAL PRIMARY KEY,
            grid_cell_id BIGINT REFERENCES grid_cells(id) ON DELETE CASCADE,
            acquisition_date DATE NOT NULL,
            ndvi NUMERIC(8, 5),
            cloud_coverage NUMERIC(5, 2),
            processing_level TEXT,
            source_product_id TEXT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT sentinel_observations_ndvi_range CHECK (ndvi IS NULL OR ndvi BETWEEN -1 AND 1),
            CONSTRAINT sentinel_observations_cloud_range CHECK (
                cloud_coverage IS NULL OR cloud_coverage BETWEEN 0 AND 100
            )
        )
        """
    )
    op.execute("CREATE INDEX sentinel_observations_grid_cell_id_idx ON sentinel_observations (grid_cell_id)")
    op.execute("CREATE INDEX sentinel_observations_acquisition_date_idx ON sentinel_observations (acquisition_date)")
    op.execute("CREATE INDEX sentinel_observations_source_product_id_idx ON sentinel_observations (source_product_id)")

    op.execute(
        """
        CREATE TABLE verification_results (
            id BIGSERIAL PRIMARY KEY,
            grid_cell_id BIGINT REFERENCES grid_cells(id) ON DELETE CASCADE,
            ogm_parcel_id BIGINT REFERENCES ogm_parcels(id) ON DELETE SET NULL,
            observation_id BIGINT REFERENCES sentinel_observations(id) ON DELETE SET NULL,
            anomaly_type TEXT NOT NULL,
            score NUMERIC(8, 5) NOT NULL,
            explanation TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT verification_results_score_range CHECK (score BETWEEN 0 AND 1)
        )
        """
    )
    op.execute("CREATE INDEX verification_results_grid_cell_id_idx ON verification_results (grid_cell_id)")
    op.execute("CREATE INDEX verification_results_ogm_parcel_id_idx ON verification_results (ogm_parcel_id)")
    op.execute("CREATE INDEX verification_results_observation_id_idx ON verification_results (observation_id)")
    op.execute("CREATE INDEX verification_results_anomaly_type_idx ON verification_results (anomaly_type)")
    op.execute("CREATE INDEX verification_results_created_at_idx ON verification_results (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS verification_results")
    op.execute("DROP TABLE IF EXISTS sentinel_observations")
    op.execute("DROP TABLE IF EXISTS ogm_parcels")
    op.execute("DROP TABLE IF EXISTS grid_cells")
    op.execute("DROP TABLE IF EXISTS regions")
