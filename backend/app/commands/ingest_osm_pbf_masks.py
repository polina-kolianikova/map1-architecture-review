from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.source_strategy import (
    SOURCE_LAYER_OSM_FOREST,
    SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
    source_definition,
)


DEFAULT_PBF_URL = "https://download.geofabrik.de/europe/turkey-latest.osm.pbf"
DEFAULT_FALLBACK_PBF_URLS = ("https://download.openstreetmap.fr/extracts/europe/turkey-latest.osm.pbf",)
DEFAULT_DOWNLOAD_DIR = "/app/artifacts/osm"
DEFAULT_OGR_TEMP_DIR = "/app/artifacts/osm/tmp"
POLYGON_LANDUSE = (
    "allotments",
    "aquaculture",
    "basin",
    "residential",
    "commercial",
    "industrial",
    "retail",
    "construction",
    "farmland",
    "farmyard",
    "grass",
    "meadow",
    "orchard",
    "plant_nursery",
    "vineyard",
    "quarry",
    "brownfield",
    "cemetery",
    "garages",
    "greenhouse_horticulture",
    "landfill",
    "railway",
    "recreation_ground",
    "reservoir",
)
POLYGON_NATURAL = (
    "bare_rock",
    "beach",
    "sand",
    "scree",
    "shingle",
    "water",
    "wetland",
)
POLYGON_WATER = (
    "basin",
    "canal",
    "ditch",
    "drain",
    "lake",
    "pond",
    "reservoir",
    "river",
    "stream",
)
POLYGON_LEISURE = (
    "garden",
    "golf_course",
    "pitch",
    "playground",
    "recreation_ground",
    "sports_centre",
    "stadium",
    "swimming_pool",
    "track",
)
POLYGON_ANY_COLUMNS = (
    "building",
    "amenity",
    "barrier",
    "man_made",
    "military",
    "aeroway",
    "tourism",
    "shop",
    "office",
    "craft",
)
FOREST_WHERE = "natural = 'wood' OR landuse = 'forest'"
NONFOREST_POLYGON_WHERE_FOR_METADATA = (
    " OR ".join(f"{column} IS NOT NULL" for column in POLYGON_ANY_COLUMNS)
    + " OR landuse IN ("
    + ",".join(f"'{value}'" for value in POLYGON_LANDUSE)
    + ")"
    + " OR natural IN ("
    + ",".join(f"'{value}'" for value in POLYGON_NATURAL)
    + ")"
    + " OR water IN ("
    + ",".join(f"'{value}'" for value in POLYGON_WATER)
    + ")"
    + " OR leisure IN ("
    + ",".join(f"'{value}'" for value in POLYGON_LEISURE)
    + ")"
)
NONFOREST_LINE_WHERE_FOR_METADATA = "highway IS NOT NULL OR railway IS NOT NULL OR waterway IS NOT NULL"
NONFOREST_POLYGON_WHERE = "1=1"
NONFOREST_LINE_WHERE = "1=1"
FOREST_TAG_FILTERS = ("wr/natural=wood", "wr/landuse=forest")
NONFOREST_POLYGON_TAG_FILTERS = (
    *(f"wr/{column}" for column in POLYGON_ANY_COLUMNS),
    *(f"wr/landuse={value}" for value in POLYGON_LANDUSE),
    *(f"wr/natural={value}" for value in POLYGON_NATURAL),
    *(f"wr/water={value}" for value in POLYGON_WATER),
    *(f"wr/leisure={value}" for value in POLYGON_LEISURE),
)
NONFOREST_LINE_TAG_FILTERS = ("w/highway", "w/railway", "w/waterway")


@dataclass(frozen=True)
class ResolvedPbf:
    path: Path
    source_url: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest public OSM PBF forest and non-forest masks")
    parser.add_argument("--region-code", required=True)
    parser.add_argument(
        "--mode",
        choices=("all", "forest", "nonforest", "nonforest-polygons", "nonforest-lines"),
        default="all",
    )
    parser.add_argument("--pbf-url", default=DEFAULT_PBF_URL)
    parser.add_argument("--fallback-pbf-url", action="append", default=list(DEFAULT_FALLBACK_PBF_URLS))
    parser.add_argument("--pbf-path", default="")
    parser.add_argument("--download-dir", default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--ogr2ogr-path", default="ogr2ogr")
    parser.add_argument("--ogr-temp-dir", default=DEFAULT_OGR_TEMP_DIR)
    parser.add_argument("--osmium-path", default="osmium")
    parser.add_argument("--projected-srid", type=int, default=6933)
    parser.add_argument("--line-buffer-m", type=float, default=8.0)
    parser.add_argument("--min-forest-area-ha", type=float, default=0.05)
    parser.add_argument("--min-exclusion-area-ha", type=float, default=0.001)
    parser.add_argument("--skip-region-clip", action="store_true")
    parser.add_argument("--no-nonforest-forest-mask-filter", action="store_true")
    parser.add_argument("--nonforest-filter-source-layers", default=SOURCE_LAYER_OSM_FOREST)
    parser.add_argument("--forest-mask-batch-size", type=int, default=1000)
    parser.add_argument("--keep-staging", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    _validate_args(args)

    with SessionLocal() as db:
        region = _load_region(db, args.region_code)

    pbf = resolve_pbf_path(
        pbf_path=args.pbf_path,
        pbf_url=args.pbf_url,
        fallback_pbf_urls=args.fallback_pbf_url,
        download_dir=args.download_dir,
        skip_download=args.skip_download,
        dry_run=args.dry_run,
        force_download=args.force_download,
    )
    if args.dry_run:
        print(
            "osm pbf mask dry run "
            f"region_code={args.region_code} mode={args.mode} pbf_path={pbf.path}"
        )
        return
    nonforest_filter_source_layers = (
        []
        if args.no_nonforest_forest_mask_filter
        else parse_source_layers(args.nonforest_filter_source_layers)
    )
    if shutil.which(args.ogr2ogr_path) is None:
        raise RuntimeError("ogr2ogr is not installed in the runtime image")

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    with SessionLocal() as db:
        stored: dict[str, int] = {}
        staging_tables: list[str] = []
        try:
            if args.mode in {"all", "forest"}:
                forest_table = staging_table_name("stage3_osm_pbf_forest", region["code"])
                staging_tables.append(forest_table)
                forest_pbf = filter_osm_pbf(
                    pbf_path=pbf.path,
                    profile="forest",
                    expressions=FOREST_TAG_FILTERS,
                    osmium_path=args.osmium_path,
                )
                imported = import_osm_layer(
                    pbf_path=forest_pbf,
                    table_name=forest_table,
                    osm_layer="multipolygons",
                    where=FOREST_WHERE,
                    ogr2ogr_path=args.ogr2ogr_path,
                    temp_dir=Path(args.ogr_temp_dir),
                )
                prepare_staging_table(db, forest_table)
                stored["forest"] = store_forest_mask(
                    db,
                    region=region,
                    staging_table=forest_table,
                    pbf_url=pbf.source_url,
                    pbf_path=pbf.path,
                    started_at=started_at,
                    imported_rows=imported,
                    min_area_ha=args.min_forest_area_ha,
                    projected_srid=args.projected_srid,
                    clip_to_region=not args.skip_region_clip,
                )
                record_source_check(
                    db,
                    source_layer=SOURCE_LAYER_OSM_FOREST,
                    source_type="osm_pbf_geofabrik",
                    region_code=region["code"],
                    status="ok" if stored["forest"] else "empty",
                    feature_count=stored["forest"],
                    pipeline="stage3_osm_forest_source_check",
                    metadata={
                        "pbf_url": args.pbf_url,
                        "effective_pbf_url": pbf.source_url,
                        "fallback_pbf_urls": args.fallback_pbf_url,
                        "pbf_path": str(pbf.path),
                        "filtered_pbf_path": str(forest_pbf),
                        "imported_rows": imported,
                        "where": FOREST_WHERE,
                    },
                )
            if args.mode in {"all", "nonforest", "nonforest-polygons", "nonforest-lines"}:
                polygon_table = staging_table_name("stage3_osm_pbf_nonforest_poly", region["code"])
                line_table = staging_table_name("stage3_osm_pbf_nonforest_line", region["code"])
                imported_polygons = 0
                imported_lines = 0
                stored_polygons = 0
                stored_lines = 0
                if args.mode in {"all", "nonforest", "nonforest-polygons"}:
                    staging_tables.append(polygon_table)
                    polygon_pbf = filter_osm_pbf(
                        pbf_path=pbf.path,
                        profile="nonforest_polygon",
                        expressions=NONFOREST_POLYGON_TAG_FILTERS,
                        osmium_path=args.osmium_path,
                    )
                    imported_polygons = import_osm_layer(
                        pbf_path=polygon_pbf,
                        table_name=polygon_table,
                        osm_layer="multipolygons",
                        where=NONFOREST_POLYGON_WHERE,
                        ogr2ogr_path=args.ogr2ogr_path,
                        temp_dir=Path(args.ogr_temp_dir),
                    )
                    prepare_staging_table(db, polygon_table)
                    stored_polygons = store_nonforest_polygon_exclusions(
                        db,
                        region=region,
                        staging_table=polygon_table,
                        pbf_url=pbf.source_url,
                        pbf_path=pbf.path,
                        started_at=started_at,
                        imported_rows=imported_polygons,
                        projected_srid=args.projected_srid,
                        min_area_ha=args.min_exclusion_area_ha,
                        clip_to_region=not args.skip_region_clip,
                        filter_source_layers=nonforest_filter_source_layers,
                        forest_batch_size=args.forest_mask_batch_size,
                    )
                    stored["nonforest_polygons"] = stored_polygons
                if args.mode in {"all", "nonforest", "nonforest-lines"}:
                    staging_tables.append(line_table)
                    line_pbf = filter_osm_pbf(
                        pbf_path=pbf.path,
                        profile="nonforest_line",
                        expressions=NONFOREST_LINE_TAG_FILTERS,
                        osmium_path=args.osmium_path,
                    )
                    imported_lines = import_osm_layer(
                        pbf_path=line_pbf,
                        table_name=line_table,
                        osm_layer="lines",
                        where=NONFOREST_LINE_WHERE,
                        ogr2ogr_path=args.ogr2ogr_path,
                        temp_dir=Path(args.ogr_temp_dir),
                    )
                    prepare_staging_table(db, line_table)
                    stored_lines = store_nonforest_line_exclusions(
                        db,
                        region=region,
                        staging_table=line_table,
                        pbf_url=pbf.source_url,
                        pbf_path=pbf.path,
                        started_at=started_at,
                        imported_rows=imported_lines,
                        projected_srid=args.projected_srid,
                        line_buffer_m=args.line_buffer_m,
                        min_area_ha=args.min_exclusion_area_ha,
                        clip_to_region=not args.skip_region_clip,
                        filter_source_layers=nonforest_filter_source_layers,
                        forest_batch_size=args.forest_mask_batch_size,
                    )
                    stored["nonforest_lines"] = stored_lines
                stored["nonforest"] = stored_polygons + stored_lines
                record_source_check(
                    db,
                    source_layer=SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
                    source_type="osm_pbf_geofabrik",
                    region_code=region["code"],
                    status="ok" if stored["nonforest"] else "empty",
                    feature_count=stored["nonforest"],
                    pipeline="stage3_osm_nonforest_exclusion_source_check",
                    metadata={
                        "pbf_url": args.pbf_url,
                        "effective_pbf_url": pbf.source_url,
                        "fallback_pbf_urls": args.fallback_pbf_url,
                        "pbf_path": str(pbf.path),
                        "components": args.mode,
                        "imported_polygon_rows": imported_polygons,
                        "imported_line_rows": imported_lines,
                        "stored_polygon_features": stored_polygons,
                        "stored_line_features": stored_lines,
                        "polygon_filter": NONFOREST_POLYGON_WHERE_FOR_METADATA,
                        "line_filter": NONFOREST_LINE_WHERE_FOR_METADATA,
                        "polygon_pbf_tag_filters": list(NONFOREST_POLYGON_TAG_FILTERS),
                        "line_pbf_tag_filters": list(NONFOREST_LINE_TAG_FILTERS),
                        "ogr_polygon_where": NONFOREST_POLYGON_WHERE,
                        "ogr_line_where": NONFOREST_LINE_WHERE,
                        "line_buffer_m": args.line_buffer_m,
                        "limited_to_forest_mask": bool(nonforest_filter_source_layers),
                        "filter_source_layers": nonforest_filter_source_layers,
                    },
                )
            if not args.keep_staging:
                drop_staging_tables(db, staging_tables)
            db.commit()
        except Exception:
            db.rollback()
            raise

    print(
        "osm pbf masks stored "
        f"region_code={args.region_code} "
        + " ".join(f"{key}_features={value}" for key, value in sorted(stored.items()))
    )


def _validate_args(args) -> None:
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.line_buffer_m < 0:
        raise ValueError("line-buffer-m must not be negative")
    if args.min_forest_area_ha < 0 or args.min_exclusion_area_ha < 0:
        raise ValueError("minimum area values must not be negative")
    if args.forest_mask_batch_size <= 0:
        raise ValueError("forest-mask-batch-size must be greater than zero")
    if not args.no_nonforest_forest_mask_filter and not parse_source_layers(args.nonforest_filter_source_layers):
        raise ValueError("nonforest-filter-source-layers must not be empty unless the filter is disabled")
    if args.skip_download and not args.pbf_path:
        raise ValueError("skip-download requires pbf-path")
    if args.osmium_path and shutil.which(args.osmium_path) is None:
        raise RuntimeError("osmium is not installed in the runtime image")


def resolve_pbf_path(
    *,
    pbf_path: str,
    pbf_url: str,
    fallback_pbf_urls: list[str] | tuple[str, ...] | None = None,
    download_dir: str,
    skip_download: bool,
    dry_run: bool,
    force_download: bool = False,
) -> ResolvedPbf:
    if pbf_path:
        path = Path(pbf_path)
        if not dry_run and not path.exists():
            raise FileNotFoundError(f"PBF file does not exist: {path}")
        return ResolvedPbf(path=path, source_url=pbf_url)
    path = Path(download_dir) / pbf_url.rstrip("/").split("/")[-1]
    if not skip_download and not dry_run:
        source_url = download_file((pbf_url, *(fallback_pbf_urls or ())), path, force=force_download)
    else:
        source_url = pbf_url
    return ResolvedPbf(path=path, source_url=source_url)


def download_file(urls: tuple[str, ...], path: Path, *, force: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0 and not force:
        return urls[0]
    if force:
        partial_path = path.with_suffix(path.suffix + ".part")
        if partial_path.exists():
            partial_path.unlink()
    errors: list[str] = []
    for url in dict.fromkeys(urls):
        try:
            _download_file_once(url, path)
            return url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("PBF download failed from all configured sources: " + " | ".join(errors))


def _download_file_once(url: str, path: Path) -> None:
    partial_path = path.with_suffix(path.suffix + ".part")
    if shutil.which("curl"):
        command = [
            "curl",
            "--fail",
            "--location",
            "--retry",
            "5",
            "--retry-delay",
            "5",
            "--connect-timeout",
            "20",
            "--continue-at",
            "-",
            "--output",
            str(partial_path),
            url,
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode == 0:
            partial_path.replace(path)
            return
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"PBF download failed: {stderr[-1000:]}")
    with requests.get(url, stream=True, timeout=(10, 90), headers={"User-Agent": "map1-turkey-stage3/1.0"}) as response:
        response.raise_for_status()
        with partial_path.open("ab") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    partial_path.replace(path)


def filter_osm_pbf(
    *,
    pbf_path: Path,
    profile: str,
    expressions: tuple[str, ...],
    osmium_path: str,
) -> Path:
    if shutil.which(osmium_path) is None:
        raise RuntimeError("osmium is not installed in the runtime image")
    output_path = pbf_path.with_name(f"{pbf_path.stem}.{profile}{pbf_path.suffix}")
    signature_path = output_path.with_name(output_path.name + ".filter.json")
    if (
        output_path.exists()
        and output_path.stat().st_size > 0
        and output_path.stat().st_mtime >= pbf_path.stat().st_mtime
        and filter_signature_matches(signature_path, profile=profile, expressions=expressions)
    ):
        return output_path
    command = [
        osmium_path,
        "tags-filter",
        str(pbf_path),
        *expressions,
        "--overwrite",
        "--output",
        str(output_path),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"osmium tag filter failed for {profile}: {stderr[-1000:]}")
    write_filter_signature(signature_path, profile=profile, expressions=expressions)
    return output_path


def filter_signature_matches(signature_path: Path, *, profile: str, expressions: tuple[str, ...]) -> bool:
    if not signature_path.exists():
        return False
    try:
        payload = json.loads(signature_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("profile") == profile and payload.get("expressions_sha256") == filter_expression_hash(expressions)


def write_filter_signature(signature_path: Path, *, profile: str, expressions: tuple[str, ...]) -> None:
    payload = {
        "profile": profile,
        "expressions": list(expressions),
        "expressions_sha256": filter_expression_hash(expressions),
    }
    signature_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def filter_expression_hash(expressions: tuple[str, ...]) -> str:
    payload = json.dumps(list(expressions), separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def import_osm_layer(
    *,
    pbf_path: Path,
    table_name: str,
    osm_layer: str,
    where: str,
    ogr2ogr_path: str,
    temp_dir: Path = Path(DEFAULT_OGR_TEMP_DIR),
) -> int:
    settings = get_settings()
    pg_dsn = (
        f"PG:host={settings.postgres_host} "
        f"port={settings.postgres_port} "
        f"dbname={settings.postgres_db} "
        f"user={settings.postgres_user}"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = settings.postgres_password
    # OSM geometry assembly can exceed a container's small /tmp filesystem.
    # Keep each import's indexes on the processing volume, with separate paths.
    temp_dir.mkdir(parents=True, exist_ok=True)
    required_bytes = max(8 * 1024**3, pbf_path.stat().st_size * 4)
    if shutil.disk_usage(temp_dir).free < required_bytes:
        raise RuntimeError("Insufficient scratch space for the OSM geometry index")
    env.setdefault("OSM_COMPRESS_NODES", "YES")
    command = [
        ogr2ogr_path,
        "-f",
        "PostgreSQL",
        pg_dsn,
        str(pbf_path),
        osm_layer,
        "-overwrite",
        "-nln",
        table_name,
        "-lco",
        "GEOMETRY_NAME=geom",
        "-lco",
        "FID=gid",
        "-t_srs",
        "EPSG:4326",
        "-where",
        where,
    ]
    with tempfile.TemporaryDirectory(prefix="ogr-", dir=temp_dir) as scratch:
        env["CPL_TMPDIR"] = scratch
        env["TMPDIR"] = scratch
        completed = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    # GDAL can return zero after index errors and still write an incomplete layer.
    diagnostics = (completed.stderr or "") + "\n" + (completed.stdout or "")
    failed_geometry = re.search(r"(?mi)^\s*(?:ERROR|FAILURE|FATAL)(?:\s+\d+)?\s*:", diagnostics)
    if completed.returncode != 0 or failed_geometry:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"ogr2ogr import failed for {osm_layer}: {stderr[-1000:]}")
    with SessionLocal() as db:
        return int(db.execute(text(f"SELECT count(*) FROM {quote_identifier(table_name)}")).scalar_one())


def prepare_staging_table(db, table_name: str) -> None:
    table = quote_identifier(table_name)
    index_name = quote_identifier(f"{table_name}_geom_gix")
    db.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} USING GIST (geom)"))
    db.execute(text(f"ANALYZE {table}"))


def store_forest_mask(
    db,
    *,
    region: dict[str, Any],
    staging_table: str,
    pbf_url: str,
    pbf_path: Path,
    started_at: str,
    imported_rows: int,
    min_area_ha: float,
    projected_srid: int,
    clip_to_region: bool,
) -> int:
    ensure_text_columns(db, staging_table, ("osm_id", "natural", "landuse", "name", "other_tags"))
    table = quote_identifier(staging_table)
    region_join = "JOIN regions region ON region.id = :region_id" if clip_to_region else ""
    geom_expression = (
        "ST_Intersection(ST_MakeValid(osm.geom), region.geom)"
        if clip_to_region
        else "ST_MakeValid(osm.geom)"
    )
    spatial_filter = "AND osm.geom && region.geom AND ST_Intersects(osm.geom, region.geom)" if clip_to_region else ""
    inserted_ids = db.execute(
        text(
            f"""
            WITH clipped AS (
                SELECT
                    osm.osm_id::text AS osm_id,
                    NULLIF(osm."natural", '') AS natural_tag,
                    NULLIF(osm.landuse, '') AS landuse_tag,
                    NULLIF(osm.name, '') AS name,
                    osm.other_tags::text AS other_tags,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid({geom_expression}),
                            3
                        )
                    ) AS geom
                FROM {table} osm
                {region_join}
                WHERE osm.geom IS NOT NULL
                  {spatial_filter}
            ),
            measurable AS (
                SELECT
                    *,
                    ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_IsValid(geom)
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
                    'osm:pbf:forest:' || COALESCE(osm_id, md5(ST_AsEWKB(geom)::text)),
                    :source_layer,
                    COALESCE(natural_tag, landuse_tag, 'forest'),
                    NULL,
                    :official_status,
                    jsonb_build_object(
                        'pipeline', 'stage3_osm_pbf_forest_mask',
                        'region_id', CAST(:region_id AS bigint),
                        'region_code', CAST(:region_code AS text),
                        'source_url', CAST(:pbf_url AS text),
                        'source_path', CAST(:pbf_path AS text),
                        'source_definition', CAST(:source_definition AS jsonb),
                        'source_checked_at', CAST(:started_at AS text),
                        'imported_rows', CAST(:imported_rows AS integer),
                        'osm_tags_used', CAST(:osm_tags_used AS jsonb),
                        'source_attributes', jsonb_build_object(
                            'osm_id', osm_id,
                            'natural', natural_tag,
                            'landuse', landuse_tag,
                            'name', name,
                            'other_tags', other_tags
                        )
                    ),
                    geom,
                    now(),
                    false
                FROM measurable
                WHERE area_ha >= :min_area_ha
                RETURNING id
            )
            SELECT COALESCE(array_agg(id), ARRAY[]::bigint[]) AS ids
            FROM inserted
            """
        ),
        {
            "region_id": region["id"],
            "region_code": region["code"],
            "projected_srid": projected_srid,
            "source_layer": SOURCE_LAYER_OSM_FOREST,
            "official_status": "public_osm_forest",
            "pbf_url": pbf_url,
            "pbf_path": str(pbf_path),
            "source_definition": json.dumps(source_definition(SOURCE_LAYER_OSM_FOREST), separators=(",", ":")),
            "started_at": started_at,
            "imported_rows": imported_rows,
            "osm_tags_used": json.dumps(["natural=wood", "landuse=forest"], separators=(",", ":")),
            "min_area_ha": min_area_ha,
        },
    ).scalar_one()
    return activate_inserted_rows(
        db,
        source_layer=SOURCE_LAYER_OSM_FOREST,
        region_code=region["code"],
        inserted_ids=inserted_ids,
    )


def store_nonforest_polygon_exclusions(
    db,
    *,
    region: dict[str, Any],
    staging_table: str,
    pbf_url: str,
    pbf_path: Path,
    started_at: str,
    imported_rows: int,
    projected_srid: int,
    min_area_ha: float,
    clip_to_region: bool,
    filter_source_layers: list[str] | None = None,
    forest_batch_size: int = 1000,
) -> int:
    if filter_source_layers is None:
        filter_source_layers = [SOURCE_LAYER_OSM_FOREST]
    polygon_columns = (
        "osm_id",
        *POLYGON_ANY_COLUMNS,
        "landuse",
        "natural",
        "water",
        "leisure",
        "name",
        "other_tags",
    )
    ensure_text_columns(db, staging_table, polygon_columns)
    table = quote_identifier(staging_table)
    polygon_source_attributes = _polygon_source_attributes_sql("")
    forest_polygon_source_attributes = _polygon_source_attributes_sql("source.")
    exclusion_type = _polygon_exclusion_type_sql("")
    forest_exclusion_type = _polygon_exclusion_type_sql("source.")
    source_sql = f"""
        SELECT
            NULL::bigint AS forest_id,
            osm_id::text AS osm_id,
            {exclusion_type} AS exclusion_type,
            {polygon_source_attributes} AS source_attributes,
            geom
        FROM {table}
    """
    forest_limited_source_sql = f"""
        SELECT
            NULL::bigint AS forest_id,
            source.osm_id::text AS osm_id,
            {forest_exclusion_type} AS exclusion_type,
            {forest_polygon_source_attributes} AS source_attributes,
            source.geom
        FROM {table} source
        WHERE EXISTS (
            SELECT 1
            FROM ogm_parcels forest
            WHERE forest.source_layer = ANY(:filter_source_layers)
              AND forest.is_current
              AND forest.attributes->>'region_code' = :region_code
              AND forest.geom IS NOT NULL
              AND source.geom && forest.geom
              AND ST_Intersects(ST_MakeValid(source.geom), forest.geom)
        )
    """
    return store_nonforest_exclusion_component(
        db,
        region=region,
        component="polygon",
        source_sql=source_sql,
        forest_limited_source_sql=forest_limited_source_sql,
        pbf_url=pbf_url,
        pbf_path=pbf_path,
        started_at=started_at,
        imported_rows=imported_rows,
        projected_srid=projected_srid,
        line_buffer_m=None,
        min_area_ha=min_area_ha,
        clip_to_region=clip_to_region,
        filter_source_layers=filter_source_layers,
        forest_batch_size=forest_batch_size,
    )


def store_nonforest_line_exclusions(
    db,
    *,
    region: dict[str, Any],
    staging_table: str,
    pbf_url: str,
    pbf_path: Path,
    started_at: str,
    imported_rows: int,
    projected_srid: int,
    line_buffer_m: float,
    min_area_ha: float,
    clip_to_region: bool,
    filter_source_layers: list[str] | None = None,
    forest_batch_size: int = 1000,
) -> int:
    if filter_source_layers is None:
        filter_source_layers = [SOURCE_LAYER_OSM_FOREST]
    ensure_text_columns(db, staging_table, ("osm_id", "highway", "railway", "waterway", "name", "other_tags"))
    table = quote_identifier(staging_table)
    # EPSG:6933 has independent monotone longitude/latitude axes. Expanding the
    # projected region envelope before transforming back is a conservative
    # prefilter, including lines just outside the region whose buffers enter it.
    candidate_filter = ""
    if clip_to_region and projected_srid == 6933:
        candidate_filter = """
        WHERE {prefix}geom && (
            SELECT ST_Transform(
                ST_Expand(
                    ST_Envelope(ST_Transform(ST_Envelope(scope.geom), 6933)),
                    :line_buffer_m + 1.0
                ),
                4326
            )
            FROM regions scope
            WHERE scope.id = :region_id
        )
        """
    source_sql = f"""
        SELECT
            NULL::bigint AS forest_id,
            osm_id::text AS osm_id,
            CASE
                WHEN NULLIF(highway, '') IS NOT NULL THEN 'road'
                WHEN NULLIF(railway, '') IS NOT NULL THEN 'railway'
                ELSE 'waterway'
            END AS exclusion_type,
            jsonb_build_object(
                'osm_id', osm_id::text,
                'highway', NULLIF(highway, ''),
                'railway', NULLIF(railway, ''),
                'waterway', NULLIF(waterway, ''),
                'name', NULLIF(name, ''),
                'other_tags', other_tags::text
            ) AS source_attributes,
            ST_Transform(
                ST_Buffer(ST_Transform(ST_MakeValid(geom), :projected_srid), :line_buffer_m),
                4326
            ) AS geom
        FROM {table}
        {candidate_filter.format(prefix='')}
    """
    forest_limited_source_sql = f"""
        SELECT
            source.forest_id,
            source.osm_id,
            source.exclusion_type,
            source.source_attributes,
            source.geom
        FROM (
            SELECT
                NULL::bigint AS forest_id,
                raw.osm_id::text AS osm_id,
                CASE
                    WHEN NULLIF(raw.highway, '') IS NOT NULL THEN 'road'
                    WHEN NULLIF(raw.railway, '') IS NOT NULL THEN 'railway'
                    ELSE 'waterway'
                END AS exclusion_type,
                jsonb_build_object(
                    'osm_id', raw.osm_id::text,
                    'highway', NULLIF(raw.highway, ''),
                    'railway', NULLIF(raw.railway, ''),
                    'waterway', NULLIF(raw.waterway, ''),
                    'name', NULLIF(raw.name, ''),
                    'other_tags', raw.other_tags::text
                ) AS source_attributes,
                ST_Transform(
                    ST_Buffer(ST_Transform(ST_MakeValid(raw.geom), :projected_srid), :line_buffer_m),
                    4326
                ) AS geom
            FROM {table} raw
            {candidate_filter.format(prefix='raw.')}
        ) source
        WHERE EXISTS (
            SELECT 1
            FROM ogm_parcels forest
            WHERE forest.source_layer = ANY(:filter_source_layers)
              AND forest.is_current
              AND forest.attributes->>'region_code' = :region_code
              AND forest.geom IS NOT NULL
              AND source.geom && forest.geom
              AND ST_Intersects(source.geom, forest.geom)
        )
    """
    return store_nonforest_exclusion_component(
        db,
        region=region,
        component="line",
        source_sql=source_sql,
        forest_limited_source_sql=forest_limited_source_sql,
        pbf_url=pbf_url,
        pbf_path=pbf_path,
        started_at=started_at,
        imported_rows=imported_rows,
        projected_srid=projected_srid,
        line_buffer_m=line_buffer_m,
        min_area_ha=min_area_ha,
        clip_to_region=clip_to_region,
        filter_source_layers=filter_source_layers,
        forest_batch_size=forest_batch_size,
    )


def _polygon_exclusion_type_sql(prefix: str) -> str:
    cases = [f"WHEN NULLIF({_qualified_column(prefix, column)}, '') IS NOT NULL THEN '{column}'" for column in POLYGON_ANY_COLUMNS]
    cases.extend(
        [
            f"WHEN NULLIF({_qualified_column(prefix, 'landuse')}, '') IS NOT NULL THEN 'landuse'",
            f"WHEN NULLIF({_qualified_column(prefix, 'natural')}, '') IS NOT NULL THEN 'natural'",
            f"WHEN NULLIF({_qualified_column(prefix, 'water')}, '') IS NOT NULL THEN 'water'",
            f"WHEN NULLIF({_qualified_column(prefix, 'leisure')}, '') IS NOT NULL THEN 'leisure'",
            f"WHEN COALESCE({_qualified_column(prefix, 'other_tags')}::text, '') LIKE '%\"water\"=>%' THEN 'water'",
            f"WHEN COALESCE({_qualified_column(prefix, 'other_tags')}::text, '') LIKE '%\"barrier\"=>%' THEN 'barrier'",
        ]
    )
    return "CASE " + " ".join(cases) + " ELSE 'nonforest' END"


def _polygon_source_attributes_sql(prefix: str) -> str:
    fields = [
        "'osm_id'",
        f"{_qualified_column(prefix, 'osm_id')}::text",
        "'landuse'",
        f"NULLIF({_qualified_column(prefix, 'landuse')}, '')",
        "'natural'",
        f"NULLIF({_qualified_column(prefix, 'natural')}, '')",
        "'water'",
        f"NULLIF({_qualified_column(prefix, 'water')}, '')",
        "'leisure'",
        f"NULLIF({_qualified_column(prefix, 'leisure')}, '')",
        "'name'",
        f"NULLIF({_qualified_column(prefix, 'name')}, '')",
        "'other_tags'",
        f"{_qualified_column(prefix, 'other_tags')}::text",
    ]
    for column in POLYGON_ANY_COLUMNS:
        fields.extend([f"'{column}'", f"NULLIF({_qualified_column(prefix, column)}, '')"])
    return "jsonb_build_object(" + ", ".join(fields) + ")"


def _qualified_column(prefix: str, column: str) -> str:
    if prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.", prefix):
        raise ValueError(f"invalid SQL qualifier prefix: {prefix}")
    return f"{prefix}{quote_identifier(column)}"


def store_nonforest_exclusion_component(
    db,
    *,
    region: dict[str, Any],
    component: str,
    source_sql: str,
    forest_limited_source_sql: str,
    pbf_url: str,
    pbf_path: Path,
    started_at: str,
    imported_rows: int,
    projected_srid: int,
    line_buffer_m: float | None,
    min_area_ha: float,
    clip_to_region: bool,
    filter_source_layers: list[str],
    forest_batch_size: int,
) -> int:
    limit_to_forest_mask = bool(filter_source_layers)
    region_join = "JOIN regions region ON region.id = :region_id" if clip_to_region else ""
    base_geom_expression = "ST_MakeValid(source.geom)"
    geom_expression = (
        f"ST_Intersection({base_geom_expression}, region.geom)"
        if clip_to_region
        else base_geom_expression
    )
    spatial_filter = "AND source.geom && region.geom" if clip_to_region else ""
    source_ctes = f"""
            source AS (
                {forest_limited_source_sql if limit_to_forest_mask else source_sql}
            ),
        """
    insert_sql = text(
        f"""
            WITH {source_ctes}
            clipped AS (
                SELECT
                    source.forest_id,
                    source.osm_id,
                    source.exclusion_type,
                    source.source_attributes,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid({geom_expression}),
                            3
                        )
                    ) AS geom
                FROM source
                {region_join}
                WHERE source.geom IS NOT NULL
                  {spatial_filter}
            ),
            measurable AS (
                SELECT
                    *,
                    ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_IsValid(geom)
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
                    'osm:pbf:nonforest:' || :component || ':' || COALESCE(forest_id::text, 'region') || ':' || exclusion_type || ':'
                        || COALESCE(osm_id, md5(ST_AsEWKB(geom)::text)),
                    :source_layer,
                    exclusion_type,
                    NULL,
                    :official_status,
                    jsonb_build_object(
                        'pipeline', 'stage3_osm_pbf_nonforest_exclusion',
                        'region_id', CAST(:region_id AS bigint),
                        'region_code', CAST(:region_code AS text),
                        'source_url', CAST(:pbf_url AS text),
                        'source_path', CAST(:pbf_path AS text),
                        'source_definition', CAST(:source_definition AS jsonb),
                        'source_checked_at', CAST(:started_at AS text),
                        'exclusion_component', CAST(:component AS text),
                        'imported_rows', CAST(:imported_rows AS integer),
                        'buffer_m', CAST(:line_buffer_m AS double precision),
                        'source_attributes', source_attributes
                    ),
                    geom,
                    now(),
                    false
                FROM measurable
                WHERE area_ha >= :min_area_ha
                RETURNING 1
            )
            SELECT count(*) AS inserted_count
            FROM inserted
            """
    )
    base_params = {
        "component": component,
        "region_id": region["id"],
        "region_code": region["code"],
        "projected_srid": projected_srid,
        "line_buffer_m": line_buffer_m,
        "filter_source_layers": filter_source_layers,
        "source_layer": SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
        "official_status": "public_osm_nonforest_exclusion",
        "pbf_url": pbf_url,
        "pbf_path": str(pbf_path),
        "source_definition": json.dumps(source_definition(SOURCE_LAYER_OSM_NONFOREST_EXCLUSION), separators=(",", ":")),
        "started_at": started_at,
        "imported_rows": imported_rows,
        "min_area_ha": min_area_ha,
    }
    total_inserted = 0
    batches = [(0, 1)]
    for forest_id_min, forest_id_max in batches:
        total_inserted += int(
            db.execute(
                insert_sql,
                {
                    **base_params,
                    "forest_id_min": forest_id_min,
                    "forest_id_max": forest_id_max,
                },
            ).scalar_one()
        )
    return activate_inserted_run(
        db,
        source_layer=SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
        region_code=region["code"],
        component=component,
        started_at=started_at,
        inserted_count=total_inserted,
    )


def forest_id_batches(db, *, region_code: str, batch_size: int, source_layers: list[str] | None = None):
    if source_layers is None:
        source_layers = [SOURCE_LAYER_OSM_FOREST]
    rows = db.execute(
        text(
            """
            WITH numbered AS (
                SELECT
                    id,
                    ((row_number() OVER (ORDER BY id) - 1) / :batch_size)::bigint AS batch_id
                FROM ogm_parcels
                WHERE source_layer = ANY(:source_layers)
                  AND is_current
                  AND attributes->>'region_code' = :region_code
            )
            SELECT min(id) AS min_id, max(id) + 1 AS max_id
            FROM numbered
            GROUP BY batch_id
            ORDER BY min(id)
            """
        ),
        {"source_layers": source_layers, "region_code": region_code, "batch_size": batch_size},
    ).mappings().all()
    for row in rows:
        if row["min_id"] is not None and row["max_id"] is not None:
            yield int(row["min_id"]), int(row["max_id"])


def parse_source_layers(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def activate_inserted_run(
    db,
    *,
    source_layer: str,
    region_code: str,
    component: str,
    started_at: str,
    inserted_count: int,
) -> int:
    db.execute(
        text(
            """
            UPDATE ogm_parcels
            SET is_current = false
            WHERE source_layer = :source_layer
              AND attributes->>'region_code' = :region_code
              AND COALESCE(attributes->>'exclusion_component', :component) = :component
            """
        ),
        {"source_layer": source_layer, "region_code": region_code, "component": component},
    )
    if inserted_count <= 0:
        return 0
    activated = db.execute(
        text(
            """
            WITH candidates AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY source_layer, source_id, attributes->>'region_code'
                        ORDER BY id
                    ) AS duplicate_rank
                FROM ogm_parcels
                WHERE source_layer = :source_layer
                  AND attributes->>'region_code' = :region_code
                  AND attributes->>'exclusion_component' = :component
                  AND attributes->>'source_checked_at' = :started_at
            ),
            activated AS (
                UPDATE ogm_parcels
                SET is_current = true
                WHERE id IN (
                    SELECT id
                    FROM candidates
                    WHERE duplicate_rank = 1
                )
                RETURNING id
            )
            SELECT count(*) FROM activated
            """
        ),
        {
            "source_layer": source_layer,
            "region_code": region_code,
            "component": component,
            "started_at": started_at,
        },
    ).scalar_one()
    return int(activated)


def activate_inserted_rows(
    db,
    *,
    source_layer: str,
    region_code: str,
    inserted_ids: list[int],
    component: str | None = None,
) -> int:
    if not inserted_ids:
        return 0
    if component:
        db.execute(
            text(
                """
                UPDATE ogm_parcels
                SET is_current = false
                WHERE source_layer = :source_layer
                  AND attributes->>'region_code' = :region_code
                  AND COALESCE(attributes->>'exclusion_component', :component) = :component
                """
            ),
            {"source_layer": source_layer, "region_code": region_code, "component": component},
        )
    else:
        db.execute(
            text(
                """
                UPDATE ogm_parcels
                SET is_current = false
                WHERE source_layer = :source_layer
                  AND attributes->>'region_code' = :region_code
                """
            ),
            {"source_layer": source_layer, "region_code": region_code},
        )
    db.execute(
        text("UPDATE ogm_parcels SET is_current = true WHERE id = ANY(:inserted_ids)"),
        {"inserted_ids": inserted_ids},
    )
    return len(inserted_ids)


def record_source_check(
    db,
    *,
    source_layer: str,
    source_type: str,
    region_code: str,
    status: str,
    feature_count: int,
    pipeline: str,
    metadata: dict[str, Any],
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
            "source_layer": source_layer,
            "source_type": source_type,
            "region_code": region_code,
            "status": status,
            "feature_count": feature_count,
            "metadata": json.dumps({"pipeline": pipeline, **metadata}, separators=(",", ":")),
        },
    )


def ensure_text_columns(db, table_name: str, column_names: tuple[str, ...]) -> None:
    table = quote_identifier(table_name)
    for column_name in column_names:
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {quote_identifier(column_name)} TEXT"))


def drop_staging_tables(db, table_names: list[str]) -> None:
    for table_name in table_names:
        db.execute(text(f"DROP TABLE IF EXISTS {quote_identifier(table_name)}"))


def staging_table_name(prefix: str, region_code: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError("staging table prefix is invalid")
    suffix = re.sub(r"[^a-z0-9]+", "_", region_code.lower()).strip("_")
    table_name = f"{prefix}_{suffix or 'region'}"
    if len(table_name) > 63:
        # PostgreSQL truncates identifiers at 63 bytes. Retain a stable suffix
        # so long region codes cannot overwrite another region's staging table.
        digest = hashlib.sha256(f"{prefix}\0{region_code}".encode()).hexdigest()[:16]
        table_name = f"{table_name[:46]}_{digest}"
    return table_name


def quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid SQL identifier: {value}")
    return '"' + value.replace('"', '""') + '"'


def _load_region(db, region_code: str) -> dict[str, Any]:
    row = db.execute(
        text("SELECT id, code FROM regions WHERE code = :region_code"),
        {"region_code": region_code},
    ).mappings().one_or_none()
    if not row:
        raise SystemExit(f"region not found: {region_code}")
    return dict(row)


if __name__ == "__main__":
    main()
