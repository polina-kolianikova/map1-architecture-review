from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.source_strategy import (
    SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
    source_definition,
)


WORLDCOVER_DATASET = "ESA_WorldCover_10m_2021_v200"
WORLDCOVER_COG_TEMPLATE = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/"
    "v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
NONFOREST_CLASSES = {
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare_sparse_vegetation",
    70: "snow_ice",
    80: "permanent_water",
}
NONFOREST_OUTPUT_CLASSES = {1: "nonforest"}
WORLDCOVER_VECTOR_RESOLUTION_M = 100


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest public ESA WorldCover non-forest pixels for Stage 3. "
            "This supplements OSM where buildings, fields, bare ground, or water are unmapped."
        )
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--projected-srid", type=int, default=6933)
    parser.add_argument("--min-area-ha", type=float, default=0.001)
    parser.add_argument("--buffer-m", type=float, default=0.0)
    parser.add_argument(
        "--bbox",
        default="",
        help=(
            "Optional min_lon,min_lat,max_lon,max_lat clip bbox. Use this when "
            "refreshing exclusions after the active region has already been "
            "refined to a forest-only mask."
        ),
    )
    parser.add_argument("--download-dir", default="/app/artifacts/worldcover")
    parser.add_argument("--gdal-translate-path", default="gdal_translate")
    parser.add_argument("--gdal-calc-path", default="gdal_calc.py")
    parser.add_argument("--gdal-warp-path", default="gdalwarp")
    parser.add_argument("--gdal-polygonize-path", default="gdal_polygonize.py")
    parser.add_argument("--ogr2ogr-path", default="ogr2ogr")
    parser.add_argument(
        "--store-existing-staging",
        action="store_true",
        help="finalize previously imported staging tables without re-running GDAL",
    )
    parser.add_argument(
        "--staging-suffix",
        default="",
        help=(
            "Optional safe suffix for WorldCover staging tables. Ordinary runs "
            "use a short timestamp suffix so stale tables from failed runs do "
            "not block ogr2ogr overwrite under the runtime DB role."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    _validate_args(args)

    tools = (
        args.gdal_translate_path,
        args.gdal_calc_path,
        args.gdal_warp_path,
        args.gdal_polygonize_path,
        args.ogr2ogr_path,
    )
    if not args.dry_run:
        missing = [tool for tool in tools if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"required geospatial tools are not installed: {', '.join(missing)}")

    clip_bbox = parse_bbox(args.bbox) if args.bbox else None
    with SessionLocal() as db:
        region = _load_region(db, args.region_code, clip_bbox=clip_bbox)
    tiles = worldcover_tiles(region["bbox"])
    if args.dry_run:
        print(
            "worldcover non-forest exclusion dry run "
            f"region_code={args.region_code} tiles={','.join(tiles)} "
            f"classes={','.join(str(value) for value in NONFOREST_CLASSES)}"
        )
        return
    if args.store_existing_staging:
        stored = store_existing_worldcover_staging(
            region=region,
            tiles=tiles,
            projected_srid=args.projected_srid,
            min_area_ha=args.min_area_ha,
            buffer_m=args.buffer_m,
            staging_suffix=args.staging_suffix,
        )
        if stored <= 0:
            raise SystemExit("no ESA WorldCover non-forest geometry was stored for the requested region")
        print(
            "worldcover non-forest exclusion stored "
            f"region_code={args.region_code} features={stored} imported_rows=existing-staging"
        )
        return

    download_dir = Path(args.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    staging_suffix = args.staging_suffix or datetime.now(UTC).strftime("%H%M%S")
    staging_tables: list[str] = []
    staging_tiles: list[str] = []
    skipped_tiles: list[str] = []
    imported_rows = 0
    with SessionLocal() as db:
        try:
            for index, tile in enumerate(tiles):
                staging_table = staging_table_name(region["code"], index, suffix=staging_suffix)
                with tempfile.TemporaryDirectory(prefix="map1-worldcover-", dir=download_dir) as temporary:
                    tile_rows = prepare_staging_table(
                        db,
                        bbox=region["bbox"],
                        tile=tile,
                        staging_table=staging_table,
                        temporary_dir=Path(temporary),
                        gdal_translate_path=args.gdal_translate_path,
                        gdal_calc_path=args.gdal_calc_path,
                        gdal_warp_path=args.gdal_warp_path,
                        gdal_polygonize_path=args.gdal_polygonize_path,
                        ogr2ogr_path=args.ogr2ogr_path,
                    )
                if tile_rows is None:
                    db.execute(text(f"DROP TABLE IF EXISTS {quote_identifier(staging_table)}"))
                    db.commit()
                    skipped_tiles.append(tile)
                    continue
                staging_tables.append(staging_table)
                staging_tiles.append(tile)
                imported_rows += tile_rows
                # ogr2ogr imports each staging table through a separate PostgreSQL
                # connection. Release the read locks from the count query before
                # the next "-overwrite" import tries to DROP/CREATE another
                # staging table from the same deterministic name set.
                db.commit()
            stored = store_worldcover_exclusions(
                db,
                region=region,
                staging_tables=staging_tables,
                started_at=started_at,
                imported_rows=imported_rows,
                projected_srid=args.projected_srid,
                min_area_ha=args.min_area_ha,
                buffer_m=args.buffer_m,
                tiles=staging_tiles,
                requested_tiles=tiles,
                skipped_tiles=skipped_tiles,
            )
            record_source_check(
                db,
                region_code=region["code"],
                status="ok" if stored else "empty",
                feature_count=stored,
                started_at=started_at,
                tiles=tiles,
                staged_tiles=staging_tiles,
                skipped_tiles=skipped_tiles,
            )
            db.commit()
            drop_staging_tables_safely(db, staging_tables)
        except Exception:
            db.rollback()
            raise

    if stored <= 0:
        raise SystemExit("no ESA WorldCover non-forest geometry was stored for the requested region")
    print(
        "worldcover non-forest exclusion stored "
        f"region_code={args.region_code} features={stored} imported_rows={imported_rows}"
    )


def store_existing_worldcover_staging(
    *,
    region: dict[str, Any],
    tiles: list[str],
    projected_srid: int,
    min_area_ha: float,
    buffer_m: float,
    staging_suffix: str = "",
) -> int:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    staging_tables: list[str] = []
    staging_tiles: list[str] = []
    skipped_tiles: list[str] = []
    imported_rows = 0
    with SessionLocal() as db:
        for index, tile in enumerate(tiles):
            staging_table = staging_table_name(region["code"], index, suffix=staging_suffix)
            if not table_exists(db, staging_table):
                skipped_tiles.append(tile)
                continue
            staging_tables.append(staging_table)
            staging_tiles.append(tile)
            imported_rows += table_row_count(db, staging_table)
        if not staging_tables:
            raise RuntimeError("no WorldCover staging tables found for final store")
        stored = store_worldcover_exclusions(
            db,
            region=region,
            staging_tables=staging_tables,
            started_at=started_at,
            imported_rows=imported_rows,
            projected_srid=projected_srid,
            min_area_ha=min_area_ha,
            buffer_m=buffer_m,
            tiles=staging_tiles,
            requested_tiles=tiles,
            skipped_tiles=skipped_tiles,
        )
        record_source_check(
            db,
            region_code=region["code"],
            status="ok" if stored else "empty",
            feature_count=stored,
            started_at=started_at,
            tiles=tiles,
            staged_tiles=staging_tiles,
            skipped_tiles=skipped_tiles,
        )
        db.commit()
        drop_staging_tables_safely(db, staging_tables)
    return stored


def _validate_args(args) -> None:
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.min_area_ha < 0:
        raise ValueError("min-area-ha must not be negative")
    if args.buffer_m < 0:
        raise ValueError("buffer-m must not be negative")
    if args.bbox:
        parse_bbox(args.bbox)
    if args.staging_suffix and not re.fullmatch(r"[A-Za-z0-9_]{1,12}", args.staging_suffix):
        raise ValueError("staging-suffix must contain 1-12 letters, digits, or underscores")


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be min_lon,min_lat,max_lon,max_lat")
    min_lon, min_lat, max_lon, max_lat = [float(part) for part in parts]
    if not (-180 <= min_lon < max_lon <= 180):
        raise ValueError("bbox longitude range is invalid")
    if not (-90 <= min_lat < max_lat <= 90):
        raise ValueError("bbox latitude range is invalid")
    return min_lon, min_lat, max_lon, max_lat


def bbox_to_wkt(bbox: tuple[float, float, float, float]) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        "POLYGON(("
        f"{min_lon} {min_lat},"
        f"{max_lon} {min_lat},"
        f"{max_lon} {max_lat},"
        f"{min_lon} {max_lat},"
        f"{min_lon} {min_lat}"
        "))"
    )


def _load_region(
    db,
    region_code: str,
    *,
    clip_bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    if clip_bbox is not None:
        row = db.execute(
            text(
                """
                SELECT id, code
                FROM regions
                WHERE code = :region_code
                """
            ),
            {"region_code": region_code},
        ).mappings().one()
        return {
            "id": row["id"],
            "code": row["code"],
            "bbox": list(clip_bbox),
            "clip_wkt": bbox_to_wkt(clip_bbox),
            "clip_source": "bbox",
        }
    row = db.execute(
        text(
            """
            SELECT
                id,
                code,
                ARRAY[
                    ST_XMin(geom),
                    ST_YMin(geom),
                    ST_XMax(geom),
                    ST_YMax(geom)
                ]::float[] AS bbox,
                ST_AsText(geom) AS clip_wkt
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).mappings().one()
    region = dict(row)
    region["clip_source"] = "region_geometry"
    return region


def worldcover_tiles(bbox: list[float] | tuple[float, float, float, float]) -> list[str]:
    min_lon, min_lat, max_lon, max_lat = bbox
    lon_starts = range(math.floor(min_lon / 3) * 3, math.floor(math.nextafter(max_lon, min_lon) / 3) * 3 + 1, 3)
    lat_starts = range(math.floor(min_lat / 3) * 3, math.floor(math.nextafter(max_lat, min_lat) / 3) * 3 + 1, 3)
    return [
        worldcover_tile_name(lat_start, lon_start)
        for lat_start in lat_starts
        for lon_start in lon_starts
    ]


def worldcover_tile_name(lat_start: int, lon_start: int) -> str:
    latitude = f"{'N' if lat_start >= 0 else 'S'}{abs(lat_start):02d}"
    longitude = f"{'E' if lon_start >= 0 else 'W'}{abs(lon_start):03d}"
    return f"{latitude}{longitude}"


def worldcover_tile_bbox(tile: str) -> tuple[float, float, float, float]:
    match = re.fullmatch(r"([NS])(\d{2})([EW])(\d{3})", tile)
    if not match:
        raise ValueError(f"invalid WorldCover tile name: {tile}")
    lat_sign, lat_value, lon_sign, lon_value = match.groups()
    min_lat = int(lat_value) * (1 if lat_sign == "N" else -1)
    min_lon = int(lon_value) * (1 if lon_sign == "E" else -1)
    return float(min_lon), float(min_lat), float(min_lon + 3), float(min_lat + 3)


def intersect_bbox(
    left: list[float] | tuple[float, float, float, float],
    right: list[float] | tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    min_lon = max(float(left[0]), float(right[0]))
    min_lat = max(float(left[1]), float(right[1]))
    max_lon = min(float(left[2]), float(right[2]))
    max_lat = min(float(left[3]), float(right[3]))
    if min_lon >= max_lon or min_lat >= max_lat:
        return None
    return min_lon, min_lat, max_lon, max_lat


def staging_table_name(region_code: str, index: int, *, suffix: str = "") -> str:
    safe_region = "".join(character.lower() if character.isalnum() else "_" for character in region_code)
    safe_suffix = f"_{suffix}" if suffix else ""
    return f"stage3_worldcover_nonforest_{safe_region}{safe_suffix}_{index}"


def quote_identifier(value: str) -> str:
    if not value or not all(character.isalnum() or character == "_" for character in value):
        raise ValueError(f"unsafe SQL identifier: {value}")
    return f'"{value}"'


def quote_sql_literal(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_:-]+", value):
        raise ValueError(f"unsafe SQL literal: {value}")
    return "'" + value.replace("'", "''") + "'"


def table_exists(db, table_name: str) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT to_regclass(:table_name) IS NOT NULL
                """
            ),
            {"table_name": table_name},
        ).scalar_one()
    )


def table_row_count(db, table_name: str) -> int:
    return int(db.execute(text(f"SELECT count(*) FROM {quote_identifier(table_name)}")).scalar_one())


def prepare_staging_table(
    db,
    *,
    bbox: list[float] | tuple[float, float, float, float],
    tile: str,
    staging_table: str,
    temporary_dir: Path,
    gdal_translate_path: str,
    gdal_calc_path: str,
    gdal_warp_path: str,
    gdal_polygonize_path: str,
    ogr2ogr_path: str,
) -> int | None:
    crop_bbox = intersect_bbox(bbox, worldcover_tile_bbox(tile))
    if crop_bbox is None:
        return 0
    min_lon, min_lat, max_lon, max_lat = crop_bbox
    crop = temporary_dir / f"{tile}-crop.tif"
    mask = temporary_dir / f"{tile}-nonforest.tif"
    coarse_mask = temporary_dir / f"{tile}-nonforest-{WORLDCOVER_VECTOR_RESOLUTION_M}m.tif"
    polygons = temporary_dir / f"{tile}-nonforest.geojson"
    source_url = WORLDCOVER_COG_TEMPLATE.format(tile=tile)
    translate_command = [
        gdal_translate_path,
        "-q",
        "-projwin",
        str(min_lon),
        str(max_lat),
        str(max_lon),
        str(min_lat),
        "-of",
        "GTiff",
        f"/vsicurl/{source_url}",
        str(crop),
    ]
    translate = subprocess.run(translate_command, check=False, capture_output=True, text=True)
    if translate.returncode:
        output = "\n".join(part for part in (translate.stdout, translate.stderr) if part)
        if "HTTP response code: 404" in output:
            return None
        raise subprocess.CalledProcessError(
            translate.returncode,
            translate_command,
            output=translate.stdout,
            stderr=translate.stderr,
        )
    subprocess.run(
        [
            gdal_calc_path,
            "-A",
            str(crop),
            f"--calc={worldcover_nonforest_calc_expression()}",
            "--NoDataValue=0",
            "--type=Byte",
            "--quiet",
            f"--outfile={mask}",
        ],
        check=True,
    )
    subprocess.run(
        [
            gdal_warp_path,
            "-q",
            "-t_srs",
            "EPSG:6933",
            "-tr",
            str(WORLDCOVER_VECTOR_RESOLUTION_M),
            str(WORLDCOVER_VECTOR_RESOLUTION_M),
            "-r",
            "max",
            "-ot",
            "Byte",
            "-srcnodata",
            "0",
            "-dstnodata",
            "0",
            str(mask),
            str(coarse_mask),
        ],
        check=True,
    )
    subprocess.run(
        [
            gdal_polygonize_path,
            "-q",
            "-8",
            str(coarse_mask),
            "-mask",
            str(coarse_mask),
            "-f",
            "GeoJSON",
            str(polygons),
            "worldcover",
            "class_code",
        ],
        check=True,
    )
    table = quote_identifier(staging_table)
    subprocess.run(
        [
            ogr2ogr_path,
            "-f",
            "PostgreSQL",
            _ogr_pg_connection(),
            str(polygons),
            "-nln",
            staging_table,
            "-nlt",
            "MULTIPOLYGON",
            "-s_srs",
            "EPSG:6933",
            "-lco",
            "GEOMETRY_NAME=geom",
            "-t_srs",
            "EPSG:4326",
            "-overwrite",
        ],
        check=True,
        env=_ogr_environment(),
    )
    return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def worldcover_nonforest_calc_expression() -> str:
    source_class_filter = "|".join(f"(A=={value})" for value in NONFOREST_CLASSES)
    return f"where({source_class_filter},1,0)"


def _ogr_pg_connection() -> str:
    from app.core.config import get_settings

    settings = get_settings()
    return (
        "PG:"
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"dbname={settings.postgres_db} user={settings.postgres_user}"
    )


def _ogr_environment() -> dict[str, str]:
    from app.core.config import get_settings

    environment = os.environ.copy()
    environment["PGPASSWORD"] = get_settings().postgres_password
    environment["OGR_GEOJSON_MAX_OBJ_SIZE"] = "0"
    return environment


def store_worldcover_exclusions(
    db,
    *,
    region: dict[str, Any],
    staging_tables: list[str],
    started_at: str,
    imported_rows: int,
    projected_srid: int,
    min_area_ha: float,
    buffer_m: float,
    tiles: list[str],
    requested_tiles: list[str],
    skipped_tiles: list[str],
) -> int:
    if not staging_tables:
        return 0
    inserted_ids: list[int] = []
    for index, table in enumerate(staging_tables):
        inserted_ids.extend(
            _insert_worldcover_tile_exclusions(
                db,
                region=region,
                staging_table=table,
                tile_index=index,
                tile_name=tiles[index],
                started_at=started_at,
                imported_rows=imported_rows,
                projected_srid=projected_srid,
                min_area_ha=min_area_ha,
                buffer_m=buffer_m,
                tiles=tiles,
                requested_tiles=requested_tiles,
                skipped_tiles=skipped_tiles,
            )
        )
        db.commit()

    db.execute(
        text(
            """
            UPDATE ogm_parcels
            SET is_current = false
            WHERE source_layer = :source_layer
              AND attributes->>'region_code' = :region_code
              AND NOT (id = ANY(:inserted_ids))
            """
        ),
        {
            "source_layer": SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
            "region_code": region["code"],
            "inserted_ids": inserted_ids,
        },
    )
    db.execute(
        text("UPDATE ogm_parcels SET is_current = true WHERE id = ANY(:inserted_ids)"),
        {"inserted_ids": inserted_ids},
    )
    return len(inserted_ids)


def _insert_worldcover_tile_exclusions(
    db,
    *,
    region: dict[str, Any],
    staging_table: str,
    tile_index: int,
    tile_name: str,
    started_at: str,
    imported_rows: int,
    projected_srid: int,
    min_area_ha: float,
    buffer_m: float,
    tiles: list[str],
    requested_tiles: list[str],
    skipped_tiles: list[str],
) -> list[int]:
    class_names = json.dumps(NONFOREST_OUTPUT_CLASSES, separators=(",", ":"))
    inserted_ids = db.execute(
        text(
            f"""
            WITH source AS (
                SELECT
                    CAST(:tile_index AS integer) AS tile_index,
                    CAST(:tile_name AS text) AS tile_name,
                    ogc_fid::bigint AS source_row_id,
                    class_code::int AS class_code,
                    geom
                FROM {quote_identifier(staging_table)}
            ),
            clip_region AS (
                SELECT ST_SetSRID(ST_GeomFromText(:clip_wkt), 4326) AS geom
            ),
            clipped AS (
                SELECT
                    source.tile_index,
                    source.tile_name,
                    source.source_row_id,
                    source.class_code,
                    CASE
                        WHEN :clip_source = 'bbox' AND :buffer_m = 0 THEN
                            ST_SetSRID(
                                ST_Multi(
                                    ST_CollectionExtract(
                                        ST_MakeValid(source.geom),
                                        3
                                    )
                                ),
                                4326
                            )
                        ELSE
                            ST_SetSRID(
                                ST_Multi(
                                    ST_CollectionExtract(
                                        ST_MakeValid(
                                            ST_Intersection(
                                                CASE
                                                    WHEN :buffer_m > 0 THEN ST_Transform(
                                                        ST_Buffer(
                                                            ST_Transform(source.geom, :projected_srid),
                                                            :buffer_m
                                                        ),
                                                        4326
                                                    )
                                                    ELSE source.geom
                                                END,
                                                clip_region.geom
                                            )
                                        ),
                                        3
                                    )
                                ),
                                4326
                            )
                    END AS geom
                FROM source
                JOIN clip_region ON true
                WHERE source.class_code = ANY(:class_codes)
                  AND (
                      :clip_source = 'bbox'
                      OR (
                          source.geom && clip_region.geom
                          AND ST_Intersects(source.geom, clip_region.geom)
                      )
                  )
            ),
            dissolved AS (
                SELECT
                    tile_index,
                    tile_name,
                    class_code,
                    ST_SetSRID(
                        ST_Multi(
                            ST_CollectionExtract(
                                ST_MakeValid(ST_UnaryUnion(ST_Collect(geom))),
                                3
                            )
                        ),
                        4326
                    ) AS geom
                FROM clipped
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                GROUP BY tile_index, tile_name, class_code
            ),
            prepared AS (
                SELECT
                    tile_index,
                    tile_name,
                    row_number() OVER (
                        PARTITION BY tile_index, tile_name, class_code
                        ORDER BY ST_XMin(component.geom), ST_YMin(component.geom), ST_Area(component.geom) DESC
                    ) AS source_row_id,
                    class_code,
                    ST_SetSRID(
                        ST_Multi(
                            ST_CollectionExtract(
                                ST_MakeValid(component.geom),
                                3
                            )
                        ),
                        4326
                    ) AS geom
                FROM dissolved
                CROSS JOIN LATERAL ST_Dump(dissolved.geom) AS component
                WHERE dissolved.geom IS NOT NULL AND NOT ST_IsEmpty(dissolved.geom)
            ),
            measurable AS (
                SELECT *, ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha
                FROM prepared
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
            ),
            inserted AS (
                INSERT INTO ogm_parcels (
                    source_id,
                    source_layer,
                    forest_type,
                    tree_age,
                    official_status,
                    attributes,
                    geom,
                    last_seen_at,
                    is_current
                )
                SELECT
                    'worldcover:2021:v200:' || :region_code || ':' || tile_name || ':' || class_code::text || ':' || source_row_id::text,
                    :source_layer,
                    COALESCE(CAST(:class_names AS jsonb)->>class_code::text, 'nonforest'),
                    NULL,
                    'public_satellite_landcover_nonforest',
                    jsonb_build_object(
                        'pipeline', 'stage3_worldcover_nonforest_exclusion',
                        'region_id', CAST(:region_id AS bigint),
                        'region_code', CAST(:region_code AS text),
                        'dataset', CAST(:dataset AS text),
                        'dataset_version', '2021_v200',
                        'source_definition', CAST(:source_definition AS jsonb),
                        'source_checked_at', CAST(:started_at AS text),
                        'class_code', class_code,
                        'class_name', COALESCE(CAST(:class_names AS jsonb)->>class_code::text, 'nonforest'),
                        'excluded_source_classes', CAST(:excluded_source_classes AS jsonb),
                        'tile', tile_name,
                        'tile_index', tile_index,
                        'source_row_id', source_row_id,
                        'tiles', CAST(:tiles AS jsonb),
                        'requested_tiles', CAST(:requested_tiles AS jsonb),
                        'skipped_tiles', CAST(:skipped_tiles AS jsonb),
                        'clip_source', CAST(:clip_source AS text),
                        'clip_bbox', CAST(:clip_bbox AS jsonb),
                        'imported_rows', CAST(:imported_rows AS integer),
                        'vector_resolution_m', CAST(:vector_resolution_m AS integer),
                        'buffer_m', CAST(:buffer_m AS double precision)
                    ),
                    geom,
                    now(),
                    false
                FROM measurable
                WHERE area_ha >= :min_area_ha
                RETURNING id
            )
            SELECT COALESCE(array_agg(id), ARRAY[]::bigint[]) FROM inserted
            """
        ),
        {
            "region_id": region["id"],
            "region_code": region["code"],
            "tile_index": tile_index,
            "tile_name": tile_name,
            "clip_wkt": region["clip_wkt"],
            "clip_source": region.get("clip_source", "region_geometry"),
            "clip_bbox": json.dumps(region["bbox"], separators=(",", ":")),
            "class_codes": list(NONFOREST_OUTPUT_CLASSES),
            "class_names": class_names,
            "source_layer": SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
            "dataset": WORLDCOVER_DATASET,
            "source_definition": json.dumps(
                source_definition(SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION),
                separators=(",", ":"),
            ),
            "excluded_source_classes": json.dumps(NONFOREST_CLASSES, separators=(",", ":")),
            "started_at": started_at,
            "tiles": json.dumps(tiles, separators=(",", ":")),
            "requested_tiles": json.dumps(requested_tiles, separators=(",", ":")),
            "skipped_tiles": json.dumps(skipped_tiles, separators=(",", ":")),
            "imported_rows": imported_rows,
            "vector_resolution_m": WORLDCOVER_VECTOR_RESOLUTION_M,
            "projected_srid": projected_srid,
            "buffer_m": buffer_m,
            "min_area_ha": min_area_ha,
        },
    ).scalar_one()
    return list(inserted_ids)


def record_source_check(
    db,
    *,
    region_code: str,
    status: str,
    feature_count: int,
    started_at: str,
    tiles: list[str],
    staged_tiles: list[str],
    skipped_tiles: list[str],
) -> None:
    db.execute(
        text(
            """
            INSERT INTO source_checks (
                source_layer,
                source_type,
                region_code,
                status,
                feature_count,
                error,
                metadata
            )
            VALUES (
                :source_layer,
                :source_type,
                :region_code,
                :status,
                :feature_count,
                NULL,
                CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "source_layer": SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
            "source_type": "esa_worldcover_public_cog",
            "region_code": region_code,
            "status": status,
            "feature_count": feature_count,
            "metadata": json.dumps(
                {
                    "pipeline": "stage3_worldcover_nonforest_exclusion_source_check",
                    "source_layer": SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
                    "region_code": region_code,
                    "dataset": WORLDCOVER_DATASET,
                    "source_checked_at": started_at,
                    "tiles": tiles,
                    "staged_tiles": staged_tiles,
                    "skipped_tiles": skipped_tiles,
                    "excluded_classes": NONFOREST_CLASSES,
                    "vector_resolution_m": WORLDCOVER_VECTOR_RESOLUTION_M,
                },
                separators=(",", ":"),
            ),
        },
    )


def drop_staging_tables(db, tables: list[str]) -> None:
    for table in tables:
        db.execute(text(f"DROP TABLE IF EXISTS {quote_identifier(table)}"))


def drop_staging_tables_safely(db, tables: list[str]) -> None:
    if not tables:
        return
    try:
        drop_staging_tables(db, tables)
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"worldcover staging cleanup skipped: {_short_error(exc)}")


def _short_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
