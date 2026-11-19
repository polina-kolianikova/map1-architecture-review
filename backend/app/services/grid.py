from sqlalchemy import text
from sqlalchemy.orm import Session

TEST_REGION_CODE = "TR-STAGE1-TEST"
TEST_REGION_NAME = "Stage 1 Test Region"
TEST_GRID_VERSION = "stage1-test-v1"
TEST_GRID_SIZE_M = 100
TEST_PROJECTED_SRID = 32636
TEST_REGION_COORDINATES = [
    [31.5800, 40.7000],
    [31.6050, 40.7000],
    [31.6050, 40.7225],
    [31.5800, 40.7225],
    [31.5800, 40.7000],
]
TEST_REGION_WKT = (
    "POLYGON(("
    "31.5800 40.7000,"
    "31.6050 40.7000,"
    "31.6050 40.7225,"
    "31.5800 40.7225,"
    "31.5800 40.7000"
    "))"
)


def generate_test_grid(db: Session) -> dict[str, int | str]:
    lock_acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"stage1_grid:{TEST_REGION_CODE}:{TEST_GRID_VERSION}"},
    ).scalar_one()
    if not lock_acquired:
        raise RuntimeError("Stage 1 grid generation is already running")

    region_id = db.execute(
        text(
            """
            INSERT INTO regions (name, code, country_code, geom, metadata)
            VALUES (
                :name,
                :code,
                'TR',
                ST_Multi(ST_GeomFromText(:wkt, 4326)),
                jsonb_build_object(
                    'stage3_region_source', 'stage1_test_region',
                    'forest_only', false,
                    'usage', 'pilot_grid_seed'
                )
            )
            ON CONFLICT (code) DO UPDATE
            SET name = EXCLUDED.name,
                geom = EXCLUDED.geom,
                metadata = EXCLUDED.metadata
            RETURNING id
            """
        ),
        {"name": TEST_REGION_NAME, "code": TEST_REGION_CODE, "wkt": TEST_REGION_WKT},
    ).scalar_one()

    db.execute(
        text(
            """
            DROP TABLE IF EXISTS pg_temp.stage1_grid_candidates;
            """
        )
    )

    db.execute(
        text(
            """
            CREATE TEMP TABLE stage1_grid_candidates
            ON COMMIT DROP AS
            WITH projected_region AS (
                SELECT ST_Transform(geom, :projected_srid) AS geom
                FROM regions
                WHERE id = :region_id
            ),
            grid AS (
                SELECT
                    square.geom,
                    square.i,
                    square.j
                FROM projected_region region,
                     ST_SquareGrid(:grid_size_m, region.geom) AS square
                WHERE ST_Contains(region.geom, ST_Centroid(square.geom))
            )
            SELECT
                (
                    substr(md5(:grid_version || ':' || i::text || ':' || j::text), 1, 8) || '-' ||
                    substr(md5(:grid_version || ':' || i::text || ':' || j::text), 9, 4) || '-' ||
                    substr(md5(:grid_version || ':' || i::text || ':' || j::text), 13, 4) || '-' ||
                    substr(md5(:grid_version || ':' || i::text || ':' || j::text), 17, 4) || '-' ||
                    substr(md5(:grid_version || ':' || i::text || ':' || j::text), 21, 12)
                )::uuid AS uuid,
                CAST(:region_id AS bigint) AS region_id,
                ST_Transform(geom, 4326)::geometry(Polygon, 4326) AS geom,
                ROUND((ST_Area(geom) / 10000.0)::numeric, 4) AS area_ha,
                CAST(:grid_version AS text) AS grid_version
            FROM grid
            """
        ),
        {
            "region_id": region_id,
            "grid_version": TEST_GRID_VERSION,
            "projected_srid": TEST_PROJECTED_SRID,
            "grid_size_m": TEST_GRID_SIZE_M,
        },
    )

    changed_cell_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells cell
            JOIN stage1_grid_candidates candidate
              ON candidate.uuid = cell.uuid
             AND candidate.grid_version = cell.grid_version
            WHERE cell.grid_version = :grid_version
              AND (
                  cell.region_id <> candidate.region_id
                  OR NOT ST_Equals(cell.geom, candidate.geom)
                  OR cell.area_ha <> candidate.area_ha
              )
            """
        ),
        {"grid_version": TEST_GRID_VERSION},
    ).scalar_one()
    if changed_cell_count:
        raise RuntimeError(
            f"grid_version {TEST_GRID_VERSION} already exists with different cell geometry"
        )

    existing_cell_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells
            WHERE region_id = :region_id
              AND grid_version = :grid_version
            """
        ),
        {"region_id": region_id, "grid_version": TEST_GRID_VERSION},
    ).scalar_one()

    missing_candidate_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells cell
            WHERE cell.region_id = :region_id
              AND cell.grid_version = :grid_version
              AND NOT EXISTS (
                  SELECT 1
                  FROM stage1_grid_candidates candidate
                  WHERE candidate.uuid = cell.uuid
                    AND candidate.grid_version = cell.grid_version
              )
            """
        ),
        {"region_id": region_id, "grid_version": TEST_GRID_VERSION},
    ).scalar_one()
    if missing_candidate_count:
        raise RuntimeError(
            f"grid_version {TEST_GRID_VERSION} already exists with a different cell set"
        )

    extra_candidate_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM stage1_grid_candidates candidate
            WHERE NOT EXISTS (
                SELECT 1
                FROM grid_cells cell
                WHERE cell.uuid = candidate.uuid
                  AND cell.grid_version = candidate.grid_version
            )
            """
        ),
    ).scalar_one()
    if existing_cell_count and extra_candidate_count:
        raise RuntimeError(
            f"grid_version {TEST_GRID_VERSION} already exists with a different cell set"
        )

    db.execute(
        text(
            """
            INSERT INTO grid_cells (uuid, region_id, geom, area_ha, grid_version)
            SELECT uuid, region_id, geom, area_ha, grid_version
            FROM stage1_grid_candidates
            ON CONFLICT (uuid, grid_version) DO NOTHING
            """
        ),
    )

    cell_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells
            WHERE region_id = :region_id
              AND grid_version = :grid_version
            """
        ),
        {"region_id": region_id, "grid_version": TEST_GRID_VERSION},
    ).scalar_one()

    return {
        "region_code": TEST_REGION_CODE,
        "grid_version": TEST_GRID_VERSION,
        "cell_count": cell_count,
    }


def generate_region_grid(
    db: Session,
    *,
    region_code: str,
    grid_version: str,
    grid_size_m: int = 100,
    projected_srid: int = TEST_PROJECTED_SRID,
) -> dict[str, int | str]:
    if not region_code.strip():
        raise ValueError("region_code is required")
    if not grid_version.strip():
        raise ValueError("grid_version is required")
    if grid_size_m <= 0:
        raise ValueError("grid_size_m must be greater than zero")

    lock_acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"grid:{region_code}:{grid_version}"},
    ).scalar_one()
    if not lock_acquired:
        raise RuntimeError("Grid generation is already running for this region and version")

    region = db.execute(
        text(
            """
            SELECT id, code
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).mappings().one()

    db.execute(text("DROP TABLE IF EXISTS pg_temp.region_grid_candidates"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE region_grid_candidates
            ON COMMIT DROP AS
            WITH projected_region AS (
                SELECT
                    ST_Transform(geom, :projected_srid) AS geom,
                    geom AS geom_4326
                FROM regions
                WHERE id = :region_id
            ),
            grid AS (
                SELECT
                    square.geom AS square_geom,
                    square.i,
                    square.j
                FROM projected_region region,
                     ST_SquareGrid(:grid_size_m, region.geom) AS square
                WHERE square.geom && region.geom
                  AND ST_Intersects(square.geom, region.geom)
            ),
            clipped AS (
                SELECT
                    i,
                    j,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(square_geom, region.geom)),
                            3
                        )
                    ) AS geom,
                    region.geom_4326
                FROM grid, projected_region region
            ),
            valid_clipped AS (
                SELECT i, j, geom, geom_4326
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            )
            SELECT
                (
                    substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 1, 8) || '-' ||
                    substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 9, 4) || '-' ||
                    substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 13, 4) || '-' ||
                    substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 17, 4) || '-' ||
                    substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 21, 12)
                )::uuid AS uuid,
                CAST(:region_id AS bigint) AS region_id,
                ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_Intersection(ST_Transform(geom, 4326), geom_4326)),
                        3
                    )
                )::geometry(Geometry, 4326) AS geom,
                ROUND((ST_Area(geom) / 10000.0)::numeric, 4) AS area_ha,
                CAST(:grid_version AS text) AS grid_version
            FROM valid_clipped
            """
        ),
        {
            "region_code": region["code"],
            "region_id": region["id"],
            "grid_version": grid_version,
            "projected_srid": projected_srid,
            "grid_size_m": grid_size_m,
        },
    )

    changed_cell_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells cell
            JOIN region_grid_candidates candidate
              ON candidate.uuid = cell.uuid
             AND candidate.grid_version = cell.grid_version
            WHERE cell.grid_version = :grid_version
              AND (
                  cell.region_id <> candidate.region_id
                  OR NOT ST_Equals(cell.geom, candidate.geom)
                  OR cell.area_ha <> candidate.area_ha
              )
            """
        ),
        {"grid_version": grid_version},
    ).scalar_one()
    if changed_cell_count:
        raise RuntimeError(f"grid_version {grid_version} already exists with different cell geometry")

    existing_cell_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells
            WHERE region_id = :region_id
              AND grid_version = :grid_version
            """
        ),
        {"region_id": region["id"], "grid_version": grid_version},
    ).scalar_one()

    missing_candidate_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells cell
            WHERE cell.region_id = :region_id
              AND cell.grid_version = :grid_version
              AND NOT EXISTS (
                  SELECT 1
                  FROM region_grid_candidates candidate
                  WHERE candidate.uuid = cell.uuid
                    AND candidate.grid_version = cell.grid_version
              )
            """
        ),
        {"region_id": region["id"], "grid_version": grid_version},
    ).scalar_one()
    if missing_candidate_count:
        raise RuntimeError(f"grid_version {grid_version} already exists with a different cell set")

    extra_candidate_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM region_grid_candidates candidate
            WHERE NOT EXISTS (
                SELECT 1
                FROM grid_cells cell
                WHERE cell.uuid = candidate.uuid
                  AND cell.grid_version = candidate.grid_version
            )
            """
        ),
    ).scalar_one()
    if existing_cell_count and extra_candidate_count:
        raise RuntimeError(f"grid_version {grid_version} already exists with a different cell set")

    db.execute(
        text(
            """
            INSERT INTO grid_cells (uuid, region_id, geom, area_ha, grid_version)
            SELECT uuid, region_id, geom, area_ha, grid_version
            FROM region_grid_candidates
            ON CONFLICT (uuid, grid_version) DO NOTHING
            """
        ),
    )

    cell_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM grid_cells
            WHERE region_id = :region_id
              AND grid_version = :grid_version
            """
        ),
        {"region_id": region["id"], "grid_version": grid_version},
    ).scalar_one()

    return {
        "region_code": region["code"],
        "grid_version": grid_version,
        "cell_count": cell_count,
    }
