from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.forest_scope import (
    clean_mask_min_component_area_ha,
    require_forest_only_region,
    validate_auxiliary_nonforest_exclusion_source_layers,
    validate_forest_mask_source_layers,
)
from app.services.grid import TEST_PROJECTED_SRID
from app.services.stage3_grid_options import (
    projected_srid_for_lon_lat,
    validate_existing_run,
    validate_generation_args,
)
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_PROJECTED_SRID,
    require_stage3_public_v9_full_grid_args,
    require_user_approved_full_turkey,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Stage 3 region grid in resumable projected tiles")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", required=True)
    parser.add_argument("--grid-size-m", type=int, default=100)
    parser.add_argument("--projected-srid", type=int, default=TEST_PROJECTED_SRID)
    parser.add_argument("--tile-size-m", type=int, default=10000)
    parser.add_argument("--max-tile-attempts", type=int, default=3)
    parser.add_argument("--max-failed-tiles", type=int, default=5)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--country-code", default="TR")
    parser.add_argument("--region-wkt-file", default="")
    parser.add_argument("--max-tiles", type=int, default=0)
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=0,
        help="per-tile PostgreSQL statement timeout; 0 keeps the database default",
    )
    parser.add_argument(
        "--exclude-source-layers",
        default="",
        help="comma-separated current non-forest source layers to subtract per generated grid tile",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--use-source-mask-cache",
        action="store_true",
        help="use stage3_source_mask_cache for forest/exclusion mask clipping when resuming full-scale public grids",
    )
    parser.add_argument(
        "--use-region-mask-components",
        action="store_true",
        help="use finalized stage3_region_mask_components as the clean mask source for grid generation",
    )
    parser.add_argument(
        "--grid-from-source-mask",
        action="store_true",
        help=(
            "derive each tile directly from source mask layers; by default the grid is generated "
            "inside the already prepared clean forest region geometry"
        ),
    )
    parser.add_argument(
        "--min-mask-component-area-ha",
        type=float,
        default=0,
        help=(
            "record/require the globally prepared clean-mask component threshold; component "
            "filtering is applied while preparing the clean forest mask, not after tile clipping"
        ),
    )
    parser.add_argument(
        "--ack-full-turkey-user-approved",
        action="store_true",
        help="required for full Turkey V10 scaling after explicit user approval",
    )
    args = parser.parse_args()
    require_stage3_public_v9_full_grid_args(
        region_code=args.region_code,
        grid_version=args.grid_version,
        grid_size_m=args.grid_size_m,
        projected_srid=args.projected_srid,
        tile_size_m=args.tile_size_m,
        use_region_mask_components=args.use_region_mask_components,
        use_source_mask_cache=args.use_source_mask_cache,
        grid_from_source_mask=args.grid_from_source_mask,
        min_mask_component_area_ha=args.min_mask_component_area_ha,
    )
    require_user_approved_full_turkey(
        approved=args.ack_full_turkey_user_approved,
        region_code=args.region_code,
        grid_version=args.grid_version,
    )
    validate_generation_args(args)
    args.exclude_source_layers = _parse_source_layers(args.exclude_source_layers)
    if args.exclude_source_layers:
        validate_auxiliary_nonforest_exclusion_source_layers(args.exclude_source_layers)
    if args.min_mask_component_area_ha < 0:
        raise ValueError("min-mask-component-area-ha must not be negative")

    with SessionLocal() as db:
        lock_key = f"stage3_grid:{args.region_code}:{args.grid_version}"
        if not args.resume:
            lock_acquired = db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": lock_key},
            ).scalar_one()
            if not lock_acquired:
                raise RuntimeError("Stage 3 grid generation is already running for this region and version")

        if args.region_wkt_file:
            raise RuntimeError(
                "region-wkt-file is handled by app.commands.run_stage3_prepare_region; "
                "prepare the forest-only region first, then run batched grid generation"
            )
        region = _region(db, args.region_code)
        if args.projected_srid == 0:
            args.projected_srid = _auto_projected_srid(db, region["id"])
        args.forest_region_metadata = require_forest_only_region(
            db,
            region_code=args.region_code,
        )
        _configure_forest_mask_grid_options(args)
        require_stage3_public_v9_full_grid_args(
            region_code=args.region_code,
            grid_version=args.grid_version,
            grid_size_m=args.grid_size_m,
            projected_srid=args.projected_srid,
            tile_size_m=args.tile_size_m,
            use_region_mask_components=args.use_region_mask_components,
            use_source_mask_cache=args.use_source_mask_cache,
            grid_from_source_mask=args.grid_from_source_mask,
            min_mask_component_area_ha=args.min_mask_component_area_ha,
            scope_area_ha=region["area_ha"],
        )
        require_user_approved_full_turkey(
            approved=args.ack_full_turkey_user_approved,
            region_code=args.region_code,
            grid_version=args.grid_version,
            scope_area_ha=region["area_ha"],
        )
        run = _ensure_run(db, region, args)
        if args.use_source_mask_cache and not args.resume:
            _validate_source_mask_cache(db, args)
        if args.adopt_existing:
            adopted = _adopt_existing_grid(db, run["id"], region["id"], args)
            if args.dry_run:
                db.rollback()
            else:
                db.commit()
            print(
                f"stage3 existing grid {'adoption dry run' if args.dry_run else 'adopted'} "
                f"region_code={args.region_code} grid_version={args.grid_version} cells={adopted}"
            )
            return
        if not args.resume:
            _ensure_tile_queue(db, run["id"], region["id"], args)
        if args.dry_run:
            total_tiles = _run_tile_count(db, run["id"])
            db.rollback()
            print(
                "stage3 batched grid dry run "
                f"region_code={args.region_code} grid_version={args.grid_version} "
                f"tile_size_m={args.tile_size_m} tiles={total_tiles}"
            )
            return
        else:
            db.commit()

    processed = 0
    inserted = 0
    failed = 0
    while True:
        with SessionLocal() as db:
            tile = _next_tile(db, args.region_code, args.grid_version)
            if not tile:
                _finish_run_if_complete(db, args.region_code, args.grid_version)
                run_status = _run_status(db, run["id"])
                db.commit()
                if run_status == "failed":
                    raise RuntimeError("Grid generation remains failed with no eligible tiles to process")
                break
            try:
                _configure_tile_statement_timeout(db, args)
                inserted_cells = _insert_tile_cells(db, tile, args)
                _mark_tile_completed(db, tile["id"], inserted_cells)
                processed += 1
                inserted += inserted_cells
            except Exception as exc:
                db.rollback()
                _mark_tile_failed(db, tile["id"], _short_error(exc))
                processed += 1
                failed += 1
            _update_run_progress(db, tile["run_id"])
            failed_total = _failed_tile_count(db, tile["run_id"])
            db.commit()
            if failed_total > args.max_failed_tiles:
                _mark_run_failed(args.region_code, args.grid_version, "Grid generation stopped after repeated tile failures")
                raise RuntimeError("Grid generation stopped after repeated tile failures")
        if args.max_tiles and processed >= args.max_tiles:
            break

    print(
        "stage3 batched grid processed "
        f"region_code={args.region_code} grid_version={args.grid_version} "
        f"processed_tiles={processed} inserted_cells={inserted} failed_tiles={failed}"
    )


def _region(db, region_code: str) -> dict[str, Any]:
    return dict(
        db.execute(
            text(
                """
                SELECT
                    id,
                    code,
                    ST_Area(ST_Transform(geom, :area_srid)) / 10000.0 AS area_ha
                FROM regions
                WHERE code = :region_code
                """
            ),
            {
                "region_code": region_code,
                "area_srid": STAGE3_PUBLIC_V9_PROJECTED_SRID,
            },
        ).mappings().one()
    )


def _auto_projected_srid(db, region_id: int) -> int:
    row = db.execute(
        text(
            """
            SELECT
                ST_X(ST_PointOnSurface(geom)) AS lon,
                ST_Y(ST_PointOnSurface(geom)) AS lat
            FROM regions
            WHERE id = :region_id
            """
        ),
        {"region_id": region_id},
    ).mappings().one()
    return projected_srid_for_lon_lat(float(row["lon"]), float(row["lat"]))


def _ensure_run(db, region: dict[str, Any], args) -> dict[str, Any]:
    region_hash = _grid_source_hash(db, region["id"], args)
    existing = db.execute(
        text(
            """
            SELECT *
            FROM stage3_grid_generation_runs
            WHERE region_id = :region_id
              AND grid_version = :grid_version
              AND status IN ('running', 'failed', 'completed')
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"region_id": region["id"], "grid_version": args.grid_version},
    ).mappings().one_or_none()
    if existing:
        if not args.resume:
            raise RuntimeError("Existing grid run found. Use --resume or finish it before starting a new run")
        validate_existing_run(dict(existing), args)
        _validate_existing_exclusion_options(dict(existing), args)
        _validate_existing_clean_mask_options(dict(existing), args)
        _validate_region_hash(dict(existing), region_hash)
        if _run_metadata(dict(existing)).get("source_geometry_fingerprint"):
            _validate_source_geometry_fingerprint(
                dict(existing), _exact_grid_source_hash(db, region["id"], args)
            )
        _validate_legacy_completed_run(db, dict(existing), region["id"], args)
        if existing["status"] == "completed":
            return dict(existing)
        if existing["status"] == "running":
            _update_run_forest_metadata(db, existing["id"], args)
            return dict(existing)
        _update_run_forest_metadata(db, existing["id"], args)
        db.execute(
            text(
                """
                UPDATE stage3_grid_generation_runs
                SET status = 'running',
                    finished_at = NULL,
                    metadata = metadata - 'error'
                WHERE id = :run_id
                """
            ),
            {"run_id": existing["id"]},
        )
        return dict(existing)
    if args.resume:
        raise RuntimeError("No existing grid run found. Start without --resume first")
    return dict(
        db.execute(
            text(
                """
                INSERT INTO stage3_grid_generation_runs (
                    region_id,
                    grid_version,
                    grid_size_m,
                    projected_srid,
                    tile_size_m,
                    metadata
                )
                VALUES (
                    :region_id,
                    :grid_version,
                    :grid_size_m,
                    :projected_srid,
                    :tile_size_m,
                    CAST(:metadata AS jsonb)
                )
                RETURNING *
                """
            ),
            {
                "region_id": region["id"],
                "grid_version": args.grid_version,
                "grid_size_m": args.grid_size_m,
                "projected_srid": args.projected_srid,
                "tile_size_m": args.tile_size_m,
                "metadata": json.dumps(
                    {
                        "region_code": region["code"],
                        "max_tile_attempts": args.max_tile_attempts,
                        "region_geometry_hash": region_hash,
                        "source_geometry_fingerprint": _exact_grid_source_hash(db, region["id"], args),
                        "source_geometry_fingerprint_algorithm": "ewkb-md5-v1",
                        "forest_only": _forest_metadata_params(args)["forest_only"],
                        "forest_mask": getattr(args, "forest_region_metadata", {}).get("forest_mask"),
                        "exclude_source_layers": list(getattr(args, "exclude_source_layers", []) or []),
                        "exclusion_method": (
                            "per_tile_st_difference" if getattr(args, "exclude_source_layers", []) else ""
                        ),
                        "grid_mask_method": _grid_mask_method(args),
                        "mask_source_layers": list(getattr(args, "mask_source_layers", []) or []),
                        "mask_intersect_source_layers": list(
                            getattr(args, "mask_intersect_source_layers", []) or []
                        ),
                        "mask_source_region_code": getattr(args, "mask_source_region_code", "") or "",
                        "clean_mask_policy": _clean_mask_policy_metadata(args),
                        "source_mask_cache": _source_mask_cache_metadata(args),
                    },
                    separators=(",", ":"),
                ),
            },
        ).mappings().one()
    )


def _region_geometry_hash(db, region_id: int) -> str:
    return str(
        db.execute(
            text(
                """
                SELECT md5(ST_AsEWKB(ST_SnapToGrid(geom, 0.0000001))::text)
                FROM regions
                WHERE id = :region_id
                """
            ),
            {"region_id": region_id},
        ).scalar_one()
    )


def _grid_source_hash(db, region_id: int, args) -> str:
    if _uses_region_mask_components(args):
        return str(
            db.execute(
                text(
                    """
                    WITH component_hashes AS (
                        SELECT
                            component_index,
                            md5(ST_AsEWKB(ST_SnapToGrid(geom_projected, 0.01))::text) AS geom_hash
                        FROM stage3_region_mask_components
                        WHERE region_id = :region_id
                          AND mask_version = :mask_version
                          AND projected_srid = :projected_srid
                    )
                    SELECT md5(COALESCE(string_agg(geom_hash, ':' ORDER BY component_index), ''))
                    FROM component_hashes
                    """
                ),
                {
                    "region_id": region_id,
                    "mask_version": _region_mask_components_version(args),
                    "projected_srid": args.projected_srid,
                },
            ).scalar_one()
        )
    return _region_geometry_hash(db, region_id)


def _validate_region_hash(run: dict[str, Any], region_hash: str) -> None:
    metadata = run.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    existing_hash = metadata.get("region_geometry_hash")
    if not existing_hash and run.get("status") != "completed":
        raise RuntimeError("Existing grid run is missing region_geometry_hash; use a new grid_version")
    if existing_hash and existing_hash != region_hash:
        raise RuntimeError("Existing grid run was created for a different region geometry; use a new grid_version")


def _run_metadata(run: dict[str, Any]) -> dict[str, Any]:
    metadata = run.get("metadata") or {}
    return json.loads(metadata) if isinstance(metadata, str) else metadata


def _exact_grid_source_hash(db, region_id: int, args) -> str:
    if _uses_region_mask_components(args):
        query = """
            SELECT md5(COALESCE(string_agg(
                component_index::text || ':' || md5(ST_AsEWKB(geom_projected)::text),
                ':' ORDER BY component_index), ''))
            FROM stage3_region_mask_components
            WHERE region_id = :region_id
              AND mask_version = :mask_version
              AND projected_srid = :projected_srid
        """
        params = {"region_id": region_id, "mask_version": _region_mask_components_version(args),
                  "projected_srid": args.projected_srid}
    else:
        query = "SELECT md5(ST_AsEWKB(geom)::text) FROM regions WHERE id = :region_id"
        params = {"region_id": region_id}
    return str(db.execute(text(query), params).scalar_one())


def _validate_source_geometry_fingerprint(run: dict[str, Any], fingerprint: str) -> None:
    metadata = _run_metadata(run)
    if metadata.get("source_geometry_fingerprint_algorithm") != "ewkb-md5-v1":
        raise RuntimeError("Unknown source geometry fingerprint algorithm; use a new grid_version")
    if metadata.get("source_geometry_fingerprint") != fingerprint:
        raise RuntimeError("Source geometry changed since grid creation; use a new grid_version")


def _validate_existing_exclusion_options(run: dict[str, Any], args) -> None:
    metadata = run.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    existing_layers = _parse_source_layers(metadata.get("exclude_source_layers") or [])
    requested_layers = _parse_source_layers(getattr(args, "exclude_source_layers", []) or [])
    if existing_layers != requested_layers:
        raise RuntimeError(
            "Existing grid run was created with different exclude_source_layers; use a new grid_version"
        )
    existing_method = metadata.get("exclusion_method") or ""
    requested_method = "per_tile_st_difference" if requested_layers else ""
    if existing_method != requested_method:
        raise RuntimeError("Existing grid run was created with a different exclusion_method; use a new grid_version")
    existing_mask_method = metadata.get("grid_mask_method") or ""
    requested_mask_method = _grid_mask_method(args)
    if not existing_mask_method and requested_mask_method and run.get("status") != "completed":
        raise RuntimeError("Existing grid run is missing grid_mask_method; use a new grid_version")
    if existing_mask_method and existing_mask_method != requested_mask_method:
        raise RuntimeError("Existing grid run was created with a different grid_mask_method; use a new grid_version")
    for key in ("mask_source_layers", "mask_intersect_source_layers"):
        existing = _parse_source_layers(metadata.get(key) or [])
        requested = _parse_source_layers(getattr(args, key, []) or [])
        if existing != requested:
            raise RuntimeError(f"Existing grid run was created with different {key}; use a new grid_version")
    existing_region_code = metadata.get("mask_source_region_code") or ""
    requested_region_code = getattr(args, "mask_source_region_code", "") or ""
    if existing_region_code and existing_region_code != requested_region_code:
        raise RuntimeError("Existing grid run was created with a different mask_source_region_code; use a new grid_version")


def _validate_existing_clean_mask_options(run: dict[str, Any], args) -> None:
    metadata = run.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    existing_policy = metadata.get("clean_mask_policy") or {}
    requested_policy = _clean_mask_policy_metadata(args)
    existing_min_area = float((existing_policy or {}).get("min_component_area_ha") or 0)
    requested_min_area = float((requested_policy or {}).get("min_component_area_ha") or 0)
    if existing_min_area != requested_min_area:
        raise RuntimeError(
            "Existing grid run was created with a different clean_mask_policy; use a new grid_version"
        )


def _validate_legacy_completed_run(db, run: dict[str, Any], region_id: int, args) -> None:
    if run.get("status") != "completed":
        return
    metadata = run.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    if metadata.get("region_geometry_hash"):
        return
    checks = _existing_grid_checks(db, region_id, args)
    if checks["cell_count"] <= 0:
        raise RuntimeError("Existing completed grid run has no cells for the selected region and grid version")
    if checks["invalid_cells"] or checks["outside_cells"] or checks["area_mismatch_cells"] or checks["coverage_ratio"] < 0.9:
        raise RuntimeError("Existing completed grid run no longer matches the selected region; use a new grid_version")


def _update_run_forest_metadata(db, run_id: int, args) -> None:
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs
            SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'forest_only', CAST(:forest_only AS boolean),
                    'forest_mask', CAST(:forest_mask AS jsonb),
                    'exclude_source_layers', CAST(:exclude_source_layers AS jsonb),
                    'exclusion_method', CAST(:exclusion_method AS text),
                    'grid_mask_method', CAST(:grid_mask_method AS text),
                    'mask_source_layers', CAST(:mask_source_layers AS jsonb),
                    'mask_intersect_source_layers', CAST(:mask_intersect_source_layers AS jsonb),
                    'mask_source_region_code', CAST(:mask_source_region_code AS text),
                    'clean_mask_policy', CAST(:clean_mask_policy AS jsonb),
                    'source_mask_cache', CAST(:source_mask_cache AS jsonb)
                )
            WHERE id = :run_id
            """
        ),
        {
            "run_id": run_id,
            **_forest_metadata_params(args),
            "exclude_source_layers": json.dumps(list(getattr(args, "exclude_source_layers", []) or [])),
            "exclusion_method": "per_tile_st_difference" if getattr(args, "exclude_source_layers", []) else "",
            "grid_mask_method": _grid_mask_method(args),
            "mask_source_layers": json.dumps(list(getattr(args, "mask_source_layers", []) or [])),
            "mask_intersect_source_layers": json.dumps(
                list(getattr(args, "mask_intersect_source_layers", []) or [])
            ),
            "mask_source_region_code": getattr(args, "mask_source_region_code", "") or "",
            "clean_mask_policy": json.dumps(_clean_mask_policy_metadata(args), separators=(",", ":")),
            "source_mask_cache": json.dumps(_source_mask_cache_metadata(args), separators=(",", ":")),
        },
    )


def _ensure_tile_queue(db, run_id: int, region_id: int, args) -> None:
    if _uses_region_mask_components(args):
        _ensure_region_mask_component_tile_queue(db, run_id, region_id, args)
        return
    if _uses_source_mask_grid(args):
        _ensure_source_mask_tile_queue(db, run_id, region_id, args)
        return
    db.execute(text("DELETE FROM stage3_grid_generation_tiles WHERE run_id = :run_id"), {"run_id": run_id})
    db.execute(
        text(
            """
            WITH projected_region AS (
                SELECT ST_Transform(geom, :projected_srid) AS geom
                FROM regions
                WHERE id = :region_id
            ),
            extent AS (
                SELECT
                    floor(ST_XMin(ST_Envelope(geom)) / :tile_size_m)::integer AS min_i,
                    floor(ST_YMin(ST_Envelope(geom)) / :tile_size_m)::integer AS min_j,
                    ceil(ST_XMax(ST_Envelope(geom)) / :tile_size_m)::integer AS max_i,
                    ceil(ST_YMax(ST_Envelope(geom)) / :tile_size_m)::integer AS max_j
                FROM projected_region
            ),
            tiles AS (
                SELECT
                    i,
                    j,
                    ST_MakeEnvelope(
                        i * :tile_size_m,
                        j * :tile_size_m,
                        (i + 1) * :tile_size_m,
                        (j + 1) * :tile_size_m,
                        :projected_srid
                    ) AS geom
                FROM extent,
                     generate_series(min_i, max_i - 1) AS i,
                     generate_series(min_j, max_j - 1) AS j
            )
            INSERT INTO stage3_grid_generation_tiles (
                run_id,
                tile_key,
                min_x,
                min_y,
                max_x,
                max_y
            )
            SELECT
                :run_id,
                i::text || ':' || j::text,
                ST_XMin(geom),
                ST_YMin(geom),
                ST_XMax(geom),
                ST_YMax(geom)
            FROM tiles
            ORDER BY i, j
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "projected_srid": args.projected_srid,
            "tile_size_m": args.tile_size_m,
        },
    )
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs
            SET total_tiles = (
                    SELECT count(*)
                    FROM stage3_grid_generation_tiles
                    WHERE run_id = :run_id
                ),
                processed_tiles = 0,
                inserted_cells = 0,
                status = 'running',
                finished_at = NULL
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id},
    )


def _ensure_region_mask_component_tile_queue(db, run_id: int, region_id: int, args) -> None:
    db.execute(text("DELETE FROM stage3_grid_generation_tiles WHERE run_id = :run_id"), {"run_id": run_id})
    db.execute(
        text(
            """
            WITH component_extent AS (
                SELECT ST_Extent(geom_projected) AS extent
                FROM stage3_region_mask_components
                WHERE region_id = :region_id
                  AND mask_version = :mask_version
                  AND projected_srid = :projected_srid
            ),
            extent AS (
                SELECT
                    floor(ST_XMin(extent) / :tile_size_m)::integer AS min_i,
                    floor(ST_YMin(extent) / :tile_size_m)::integer AS min_j,
                    ceil(ST_XMax(extent) / :tile_size_m)::integer AS max_i,
                    ceil(ST_YMax(extent) / :tile_size_m)::integer AS max_j
                FROM component_extent
                WHERE extent IS NOT NULL
            ),
            tiles AS (
                SELECT
                    i,
                    j,
                    ST_MakeEnvelope(
                        i * :tile_size_m,
                        j * :tile_size_m,
                        (i + 1) * :tile_size_m,
                        (j + 1) * :tile_size_m,
                        :projected_srid
                    ) AS geom
                FROM extent,
                     generate_series(min_i, max_i - 1) AS i,
                     generate_series(min_j, max_j - 1) AS j
            )
            INSERT INTO stage3_grid_generation_tiles (
                run_id,
                tile_key,
                min_x,
                min_y,
                max_x,
                max_y
            )
            SELECT
                :run_id,
                i::text || ':' || j::text,
                ST_XMin(geom),
                ST_YMin(geom),
                ST_XMax(geom),
                ST_YMax(geom)
            FROM tiles
            WHERE EXISTS (
                SELECT 1
                FROM stage3_region_mask_components component
                WHERE component.region_id = :region_id
                  AND component.mask_version = :mask_version
                  AND component.projected_srid = :projected_srid
                  AND component.geom_projected && tiles.geom
                  AND ST_Intersects(component.geom_projected, tiles.geom)
            )
            ORDER BY i, j
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "mask_version": _region_mask_components_version(args),
            "projected_srid": args.projected_srid,
            "tile_size_m": args.tile_size_m,
        },
    )
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs
            SET total_tiles = (
                    SELECT count(*)
                    FROM stage3_grid_generation_tiles
                    WHERE run_id = :run_id
                ),
                processed_tiles = 0,
                inserted_cells = 0,
                status = 'running',
                finished_at = NULL
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id},
    )


def _ensure_source_mask_tile_queue(db, run_id: int, region_id: int, args) -> None:
    db.execute(text("DELETE FROM stage3_grid_generation_tiles WHERE run_id = :run_id"), {"run_id": run_id})
    db.execute(
        text(
            """
            WITH region_extent_4326 AS (
                SELECT ST_MakeEnvelope(
                    ST_XMin(geom),
                    ST_YMin(geom),
                    ST_XMax(geom),
                    ST_YMax(geom),
                    4326
                ) AS geom
                FROM regions
                WHERE code = :mask_source_region_code
            ),
            projected_extent AS (
                SELECT ST_Transform(geom, :projected_srid) AS geom
                FROM region_extent_4326
            ),
            extent AS (
                SELECT
                    floor(ST_XMin(geom) / :tile_size_m)::integer AS min_i,
                    floor(ST_YMin(geom) / :tile_size_m)::integer AS min_j,
                    ceil(ST_XMax(geom) / :tile_size_m)::integer AS max_i,
                    ceil(ST_YMax(geom) / :tile_size_m)::integer AS max_j
                FROM projected_extent
            ),
            tiles AS (
                SELECT
                    i,
                    j,
                    ST_MakeEnvelope(
                        i * :tile_size_m,
                        j * :tile_size_m,
                        (i + 1) * :tile_size_m,
                        (j + 1) * :tile_size_m,
                        :projected_srid
                    ) AS geom
                FROM extent,
                     generate_series(min_i, max_i - 1) AS i,
                     generate_series(min_j, max_j - 1) AS j
            )
            INSERT INTO stage3_grid_generation_tiles (
                run_id,
                tile_key,
                min_x,
                min_y,
                max_x,
                max_y
            )
            SELECT
                :run_id,
                i::text || ':' || j::text,
                ST_XMin(geom),
                ST_YMin(geom),
                ST_XMax(geom),
                ST_YMax(geom)
            FROM tiles
            WHERE EXISTS (
                    SELECT 1
                    FROM ogm_parcels parcel
                    WHERE parcel.is_current
                      AND parcel.source_layer = ANY(:mask_source_layers)
                      AND parcel.attributes->>'region_code' = :mask_source_region_code
                      AND parcel.geom && ST_Transform(tiles.geom, 4326)
                      AND ST_Intersects(parcel.geom, ST_Transform(tiles.geom, 4326))
                )
              AND (
                    NOT :use_intersect_mask
                    OR EXISTS (
                        SELECT 1
                        FROM ogm_parcels parcel
                        WHERE parcel.is_current
                          AND parcel.source_layer = ANY(:mask_intersect_source_layers)
                          AND parcel.attributes->>'region_code' = :mask_source_region_code
                          AND parcel.geom && ST_Transform(tiles.geom, 4326)
                          AND ST_Intersects(parcel.geom, ST_Transform(tiles.geom, 4326))
                    )
                )
            ORDER BY i, j
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "projected_srid": args.projected_srid,
            "tile_size_m": args.tile_size_m,
            "mask_source_layers": list(getattr(args, "mask_source_layers", []) or []),
            "mask_intersect_source_layers": list(getattr(args, "mask_intersect_source_layers", []) or []),
            "mask_source_region_code": getattr(args, "mask_source_region_code", "") or args.region_code,
            "use_intersect_mask": bool(getattr(args, "mask_intersect_source_layers", [])),
        },
    )
    if bool(getattr(args, "use_source_mask_cache", False)):
        db.execute(
            text(
                """
                DELETE FROM stage3_grid_generation_tiles tile
                WHERE tile.run_id = :run_id
                  AND NOT EXISTS (
                      SELECT 1
                      FROM stage3_source_mask_cache cache
                      WHERE cache.source_region_code = :mask_source_region_code
                        AND cache.projected_srid = :projected_srid
                        AND cache.source_layer = ANY(:mask_source_layers)
                        AND cache.geom_projected && ST_MakeEnvelope(
                            tile.min_x,
                            tile.min_y,
                            tile.max_x,
                            tile.max_y,
                            :projected_srid
                        )
                        AND ST_Intersects(
                            cache.geom_projected,
                            ST_MakeEnvelope(
                                tile.min_x,
                                tile.min_y,
                                tile.max_x,
                                tile.max_y,
                                :projected_srid
                            )
                        )
                  )
                """
            ),
            {
                "run_id": run_id,
                "projected_srid": args.projected_srid,
                "mask_source_layers": list(getattr(args, "mask_source_layers", []) or []),
                "mask_source_region_code": getattr(args, "mask_source_region_code", "") or args.region_code,
            },
        )
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs
            SET total_tiles = (
                    SELECT count(*)
                    FROM stage3_grid_generation_tiles
                    WHERE run_id = :run_id
                ),
                processed_tiles = 0,
                inserted_cells = 0,
                status = 'running',
                finished_at = NULL
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id},
    )


def _run_tile_count(db, run_id: int) -> int:
    return db.execute(
        text(
            """
            SELECT count(*)
            FROM stage3_grid_generation_tiles
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id},
    ).scalar_one()


def _adopt_existing_grid(db, run_id: int, region_id: int, args) -> int:
    real_tile_count = int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM stage3_grid_generation_tiles
                WHERE run_id = :run_id
                  AND tile_key <> 'existing-grid'
                """
            ),
            {"run_id": run_id},
        ).scalar_one()
    )
    if real_tile_count:
        raise RuntimeError("Existing grid adoption cannot replace a real tile execution history")

    checks = _existing_grid_checks(db, region_id, args)
    existing_cells = checks["cell_count"]
    if existing_cells <= 0:
        raise RuntimeError("No existing grid cells found for the selected region and grid version")
    if checks["invalid_cells"]:
        raise RuntimeError("Existing grid contains invalid or empty geometries")
    if checks["outside_cells"]:
        raise RuntimeError("Existing grid has cells outside the selected region")
    if checks["area_mismatch_cells"]:
        raise RuntimeError("Existing grid contains zero-area or oversized cells")
    if checks["coverage_ratio"] < 0.9:
        raise RuntimeError("Existing grid does not cover enough of the selected region")
    region_hash = _grid_source_hash(db, region_id, args)
    source_fingerprint = _exact_grid_source_hash(db, region_id, args)
    db.execute(text("DELETE FROM stage3_grid_generation_tiles WHERE run_id = :run_id"), {"run_id": run_id})
    db.execute(
        text(
            """
            INSERT INTO stage3_grid_generation_tiles (
                run_id,
                tile_key,
                min_x,
                min_y,
                max_x,
                max_y,
                status,
                inserted_cells,
                processed_at
            )
            SELECT
                :run_id,
                'existing-grid',
                ST_XMin(ST_Envelope(ST_Transform(region.geom, :projected_srid))),
                ST_YMin(ST_Envelope(ST_Transform(region.geom, :projected_srid))),
                ST_XMax(ST_Envelope(ST_Transform(region.geom, :projected_srid))),
                ST_YMax(ST_Envelope(ST_Transform(region.geom, :projected_srid))),
                'completed',
                :existing_cells,
                now()
            FROM regions region
            WHERE region.id = :region_id
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "projected_srid": args.projected_srid,
            "existing_cells": existing_cells,
        },
    )
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs
            SET total_tiles = 1,
                processed_tiles = 1,
                inserted_cells = :existing_cells,
                status = 'completed',
                finished_at = now(),
                metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'region_geometry_hash', CAST(:region_geometry_hash AS text),
                    'source_geometry_fingerprint', CAST(:source_geometry_fingerprint AS text),
                    'source_geometry_fingerprint_algorithm', 'ewkb-md5-v1',
                    'adopted_existing_grid_cells', CAST(:existing_cells AS integer),
                    'adoption_checks', CAST(:adoption_checks AS jsonb),
                    'forest_only', CAST(:forest_only AS boolean),
                    'forest_mask', CAST(:forest_mask AS jsonb)
                )
            WHERE id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "region_geometry_hash": region_hash,
            "source_geometry_fingerprint": source_fingerprint,
            "existing_cells": existing_cells,
            "adoption_checks": json.dumps(checks, separators=(",", ":")),
            **_forest_metadata_params(args),
        },
    )
    return existing_cells


def _forest_metadata_params(args) -> dict[str, Any]:
    metadata = getattr(args, "forest_region_metadata", {}) or {}
    return {
        "forest_only": bool(metadata.get("forest_only")),
        "forest_mask": json.dumps(metadata.get("forest_mask"), separators=(",", ":")),
    }


def _existing_grid_checks(db, region_id: int, args) -> dict[str, Any]:
    expected_area_ha = (args.grid_size_m * args.grid_size_m) / 10_000.0
    row = db.execute(
        text(
            """
            WITH region AS (
                SELECT geom
                FROM regions
                WHERE id = :region_id
            ),
            cells AS (
                SELECT geom, area_ha
                FROM grid_cells
                WHERE region_id = :region_id
                  AND grid_version = :grid_version
            ),
            unioned AS (
                SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM cells
            )
            SELECT
                (SELECT count(*) FROM cells)::int AS cell_count,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE NOT ST_IsValid(cells.geom) OR ST_IsEmpty(cells.geom)
                )::int AS invalid_cells,
                (
                    SELECT count(*)
                    FROM cells, region
                    WHERE NOT ST_CoveredBy(cells.geom, region.geom)
                )::int AS outside_cells,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE cells.area_ha <= 0
                       OR cells.area_ha > (:expected_area_ha * 1.01)
                )::int AS area_mismatch_cells,
                COALESCE(
                    ST_Area(ST_Intersection(region.geom, unioned.geom)::geography)
                    / NULLIF(ST_Area(region.geom::geography), 0),
                    0
                )::float AS coverage_ratio
            FROM region
            CROSS JOIN unioned
            """
        ),
        {
            "region_id": region_id,
            "grid_version": args.grid_version,
            "expected_area_ha": expected_area_ha,
        },
    ).mappings().one()
    return {
        "cell_count": int(row["cell_count"] or 0),
        "invalid_cells": int(row["invalid_cells"] or 0),
        "outside_cells": int(row["outside_cells"] or 0),
        "area_mismatch_cells": int(row["area_mismatch_cells"] or 0),
        "coverage_ratio": round(float(row["coverage_ratio"] or 0), 5),
    }


def _failed_tile_count(db, run_id: int) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM stage3_grid_generation_tiles
                WHERE run_id = :run_id
                  AND status = 'failed'
                """
            ),
            {"run_id": run_id},
        ).scalar_one()
    )


def _next_tile(db, region_code: str, grid_version: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT tile.*, run.region_id, run.grid_version, run.grid_size_m, run.projected_srid
            FROM stage3_grid_generation_tiles tile
            JOIN stage3_grid_generation_runs run ON run.id = tile.run_id
            JOIN regions region ON region.id = run.region_id
            WHERE region.code = :region_code
              AND run.grid_version = :grid_version
              AND run.status = 'running'
              AND tile.status IN ('pending', 'failed')
              AND tile.attempt_count < COALESCE((run.metadata->>'max_tile_attempts')::integer, 3)
            ORDER BY tile.status = 'failed', tile.id
            LIMIT 1
            FOR UPDATE OF tile SKIP LOCKED
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _insert_tile_cells(db, tile: dict[str, Any], args) -> int:
    if _uses_region_mask_components(args):
        return _insert_tile_cells_from_region_mask_components(db, tile, args)
    if _uses_source_mask_grid(args):
        if _uses_source_mask_cache(args):
            return _insert_tile_cells_from_source_mask_cache(db, tile, args)
        return _insert_tile_cells_from_source_mask(db, tile, args)
    return db.execute(
        text(
            f"""
            WITH region AS (
                SELECT
                    id,
                    code,
                    geom AS geom_4326,
                    ST_Transform(geom, :projected_srid) AS geom
                FROM regions
                WHERE id = :region_id
            ),
            tile AS (
                SELECT ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, :projected_srid) AS geom
            ),
            tile_4326 AS (
                SELECT ST_Transform(geom, 4326) AS geom
                FROM tile
            ),
            exclusion_mask AS (
                SELECT ST_Transform(ST_UnaryUnion(ST_Collect(ST_MakeValid(parcel.geom))), :projected_srid) AS geom
                FROM ogm_parcels parcel, tile_4326
                WHERE :use_exclusion_mask
                  AND parcel.is_current
                  AND parcel.source_layer = ANY(:exclude_source_layers)
                  AND parcel.attributes->>'region_code' = :region_code
                  AND parcel.geom && tile_4326.geom
                  AND ST_Intersects(parcel.geom, tile_4326.geom)
            ),
            candidates AS (
                SELECT
                    square.geom AS square_geom,
                    square.i,
                    square.j
                FROM region, tile,
                     ST_SquareGrid(:grid_size_m, ST_Intersection(region.geom, tile.geom)) AS square
                WHERE square.geom && region.geom
                  AND ST_Intersects(square.geom, region.geom)
            ),
            clipped AS (
                SELECT
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                CASE
                                    WHEN :use_exclusion_mask AND exclusion_mask.geom IS NOT NULL
                                    THEN ST_Difference(ST_Intersection(square_geom, region.geom), exclusion_mask.geom)
                                    ELSE ST_Intersection(square_geom, region.geom)
                                END
                            ),
                            3
                        )
                    ) AS geom,
                    i,
                    j
                FROM candidates
                CROSS JOIN region
                LEFT JOIN exclusion_mask ON :use_exclusion_mask
            ),
            valid_clipped AS (
                SELECT geom, i, j
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            ),
            region_clipped AS (
                SELECT
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(ST_Transform(valid_clipped.geom, 4326), region.geom_4326)),
                            3
                        )
                    )::geometry(Geometry, 4326) AS geom,
                    i,
                    j
                FROM valid_clipped, region
            ),
            prepared AS (
                SELECT
                    (
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 1, 8) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 9, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 13, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 17, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 21, 12)
                    )::uuid AS uuid,
                    :region_id AS region_id,
                    {_grid_cell_geom_sql('geom')},
                    {_grid_cell_area_ha_sql('ST_Transform(geom, :projected_srid)')},
                    :grid_version AS grid_version
                FROM region_clipped
            ),
            valid_prepared AS (
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM prepared
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND area_ha > 0
            ),
            changed AS (
                SELECT count(*) AS count
                FROM grid_cells cell
                JOIN valid_prepared candidate
                  ON candidate.uuid = cell.uuid
                 AND candidate.grid_version = cell.grid_version
                WHERE cell.region_id <> candidate.region_id
                   OR NOT ST_Equals(cell.geom, candidate.geom)
                   OR cell.area_ha <> candidate.area_ha
            ),
            inserted AS (
                INSERT INTO grid_cells (uuid, region_id, geom, area_ha, grid_version)
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM valid_prepared
                WHERE (SELECT count FROM changed) = 0
                ON CONFLICT (uuid, grid_version) DO NOTHING
                RETURNING 1
            )
            SELECT
                CASE
                    WHEN (SELECT count FROM changed) > 0 THEN -1
                    ELSE (SELECT count(*) FROM inserted)
                END AS inserted_cells
            """
        ),
        {
            "region_code": args.region_code,
            "region_id": tile["region_id"],
            "grid_version": tile["grid_version"],
            "grid_size_m": tile["grid_size_m"],
            "projected_srid": tile["projected_srid"],
            "min_x": tile["min_x"],
            "min_y": tile["min_y"],
            "max_x": tile["max_x"],
            "max_y": tile["max_y"],
            "use_exclusion_mask": bool(getattr(args, "exclude_source_layers", [])),
            "exclude_source_layers": list(getattr(args, "exclude_source_layers", []) or []),
        },
    ).scalar_one()


def _grid_cell_geom_sql(source_geom_sql: str) -> str:
    return f"""
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Transform({source_geom_sql}, 4326)),
                            3
                        )
                    )::geometry(Geometry, 4326) AS geom
    """.rstrip()


def _grid_cell_area_ha_sql(projected_geom_sql: str) -> str:
    return f"""
                    GREATEST(
                        ROUND((
                            ST_Area(
                                ST_Transform(
                                    ST_Multi(
                                        ST_CollectionExtract(
                                            ST_MakeValid(ST_Transform({projected_geom_sql}, 4326)),
                                            3
                                        )
                                    ),
                                    :projected_srid
                                )
                            ) / 10000.0
                        )::numeric, 6),
                        0.000001
                    ) AS area_ha
    """.rstrip()


def _insert_tile_cells_from_source_mask(db, tile: dict[str, Any], args) -> int:
    return db.execute(
        text(
            f"""
            WITH tile AS (
                SELECT ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, :projected_srid) AS geom
            ),
            tile_4326 AS (
                SELECT ST_Transform(geom, 4326) AS geom
                FROM tile
            ),
            canonical_region_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_Intersection(
                                ST_Transform(ST_MakeValid(region.geom), :projected_srid),
                                tile.geom
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM regions region, tile
                WHERE region.id = :region_id
            ),
            source_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_UnaryUnion(
                                ST_Collect(
                                    ST_Intersection(
                                        ST_Transform(ST_MakeValid(parcel.geom), :projected_srid),
                                        tile.geom
                                    )
                                )
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel, tile, tile_4326
                WHERE parcel.is_current
                  AND parcel.source_layer = ANY(:mask_source_layers)
                  AND parcel.attributes->>'region_code' = :mask_source_region_code
                  AND parcel.geom && tile_4326.geom
                  AND ST_Intersects(parcel.geom, tile_4326.geom)
            ),
            intersect_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_UnaryUnion(
                                ST_Collect(
                                    ST_Intersection(
                                        ST_Transform(ST_MakeValid(parcel.geom), :projected_srid),
                                        tile.geom
                                    )
                                )
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel, tile, tile_4326
                WHERE :use_intersect_mask
                  AND parcel.is_current
                  AND parcel.source_layer = ANY(:mask_intersect_source_layers)
                  AND parcel.attributes->>'region_code' = :mask_source_region_code
                  AND parcel.geom && tile_4326.geom
                  AND ST_Intersects(parcel.geom, tile_4326.geom)
            ),
            forest_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            CASE
                                WHEN :use_intersect_mask
                                  AND intersect_mask.geom IS NOT NULL
                                  AND NOT ST_IsEmpty(intersect_mask.geom)
                                THEN ST_Intersection(source_mask.geom, intersect_mask.geom)
                                WHEN :use_intersect_mask
                                THEN NULL
                                ELSE source_mask.geom
                            END
                        ),
                        3
                    )
                ) AS geom
                FROM source_mask
                LEFT JOIN intersect_mask ON :use_intersect_mask
                WHERE source_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source_mask.geom)
            ),
            exclusion_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_UnaryUnion(
                                ST_Collect(
                                    ST_Intersection(
                                        ST_Transform(ST_MakeValid(parcel.geom), :projected_srid),
                                        tile.geom
                                    )
                                )
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel, tile, tile_4326
                WHERE :use_exclusion_mask
                  AND parcel.is_current
                  AND parcel.source_layer = ANY(:exclude_source_layers)
                  AND parcel.attributes->>'region_code' = :mask_source_region_code
                  AND parcel.geom && tile_4326.geom
                  AND ST_Intersects(parcel.geom, tile_4326.geom)
            ),
            tile_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            CASE
                                WHEN :use_exclusion_mask
                                  AND exclusion_mask.geom IS NOT NULL
                                  AND NOT ST_IsEmpty(exclusion_mask.geom)
                                THEN ST_Difference(forest_mask.geom, exclusion_mask.geom)
                                ELSE forest_mask.geom
                            END
                        ),
                        3
                    )
                ) AS geom
                FROM forest_mask
                LEFT JOIN exclusion_mask ON :use_exclusion_mask
                WHERE forest_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(forest_mask.geom)
            ),
            clean_tile_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_Intersection(tile_mask.geom, canonical_region_mask.geom)),
                        3
                    )
                ) AS geom
                FROM tile_mask
                JOIN canonical_region_mask
                  ON canonical_region_mask.geom IS NOT NULL
                 AND NOT ST_IsEmpty(canonical_region_mask.geom)
                WHERE tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(tile_mask.geom)
            ),
            candidates AS (
                SELECT
                    square.geom AS square_geom,
                    square.i,
                    square.j
                FROM clean_tile_mask,
                     ST_SquareGrid(:grid_size_m, clean_tile_mask.geom) AS square
                WHERE clean_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(clean_tile_mask.geom)
                  AND square.geom && clean_tile_mask.geom
                  AND ST_Intersects(square.geom, clean_tile_mask.geom)
            ),
            clipped AS (
                SELECT
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(square_geom, clean_tile_mask.geom)),
                            3
                        )
                    ) AS geom,
                    i,
                    j
                FROM candidates, clean_tile_mask
            ),
            valid_clipped AS (
                SELECT geom, i, j
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            ),
            prepared AS (
                SELECT
                    (
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 1, 8) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 9, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 13, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 17, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 21, 12)
                    )::uuid AS uuid,
                    :region_id AS region_id,
                    {_grid_cell_geom_sql('valid_clipped.geom')},
                    {_grid_cell_area_ha_sql('valid_clipped.geom')},
                    :grid_version AS grid_version
                FROM valid_clipped
            ),
            valid_prepared AS (
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM prepared
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            changed AS (
                SELECT count(*) AS count
                FROM grid_cells cell
                JOIN valid_prepared candidate
                  ON candidate.uuid = cell.uuid
                 AND candidate.grid_version = cell.grid_version
                WHERE cell.region_id <> candidate.region_id
                   OR NOT ST_Equals(cell.geom, candidate.geom)
                   OR cell.area_ha <> candidate.area_ha
            ),
            inserted AS (
                INSERT INTO grid_cells (uuid, region_id, geom, area_ha, grid_version)
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM valid_prepared
                WHERE (SELECT count FROM changed) = 0
                ON CONFLICT (uuid, grid_version) DO NOTHING
                RETURNING 1
            )
            SELECT
                CASE
                    WHEN (SELECT count FROM changed) > 0 THEN -1
                    ELSE (SELECT count(*) FROM inserted)
                END AS inserted_cells
            """
        ),
        {
            "region_code": args.region_code,
            "region_id": tile["region_id"],
            "grid_version": tile["grid_version"],
            "grid_size_m": tile["grid_size_m"],
            "projected_srid": tile["projected_srid"],
            "min_x": tile["min_x"],
            "min_y": tile["min_y"],
            "max_x": tile["max_x"],
            "max_y": tile["max_y"],
            "mask_source_layers": list(getattr(args, "mask_source_layers", []) or []),
            "mask_intersect_source_layers": list(getattr(args, "mask_intersect_source_layers", []) or []),
            "mask_source_region_code": getattr(args, "mask_source_region_code", "") or args.region_code,
            "use_intersect_mask": bool(getattr(args, "mask_intersect_source_layers", [])),
            "use_exclusion_mask": bool(getattr(args, "exclude_source_layers", [])),
            "exclude_source_layers": list(getattr(args, "exclude_source_layers", []) or []),
        },
    ).scalar_one()


def _insert_tile_cells_from_source_mask_cache(db, tile: dict[str, Any], args) -> int:
    return db.execute(
        text(
            f"""
            WITH tile AS (
                SELECT ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, :projected_srid) AS geom
            ),
            canonical_region_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_Intersection(
                                ST_Transform(ST_MakeValid(region.geom), :projected_srid),
                                tile.geom
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM regions region, tile
                WHERE region.id = :region_id
            ),
            source_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_UnaryUnion(
                                ST_Collect(
                                    ST_Intersection(cache.geom_projected, tile.geom)
                                )
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM stage3_source_mask_cache cache, tile
                WHERE cache.projected_srid = :projected_srid
                  AND cache.source_region_code = :mask_source_region_code
                  AND cache.source_layer = ANY(:mask_source_layers)
                  AND cache.geom_projected && tile.geom
                  AND ST_Intersects(cache.geom_projected, tile.geom)
            ),
            intersect_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_UnaryUnion(
                                ST_Collect(
                                    ST_Intersection(cache.geom_projected, tile.geom)
                                )
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM stage3_source_mask_cache cache, tile
                WHERE :use_intersect_mask
                  AND cache.projected_srid = :projected_srid
                  AND cache.source_region_code = :mask_source_region_code
                  AND cache.source_layer = ANY(:mask_intersect_source_layers)
                  AND cache.geom_projected && tile.geom
                  AND ST_Intersects(cache.geom_projected, tile.geom)
            ),
            forest_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            CASE
                                WHEN :use_intersect_mask
                                  AND intersect_mask.geom IS NOT NULL
                                  AND NOT ST_IsEmpty(intersect_mask.geom)
                                THEN ST_Intersection(source_mask.geom, intersect_mask.geom)
                                WHEN :use_intersect_mask
                                THEN NULL
                                ELSE source_mask.geom
                            END
                        ),
                        3
                    )
                ) AS geom
                FROM source_mask
                LEFT JOIN intersect_mask ON :use_intersect_mask
                WHERE source_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source_mask.geom)
            ),
            exclusion_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_UnaryUnion(
                                ST_Collect(
                                    ST_Intersection(cache.geom_projected, tile.geom)
                                )
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM stage3_source_mask_cache cache, tile
                WHERE :use_exclusion_mask
                  AND cache.projected_srid = :projected_srid
                  AND cache.source_region_code = :mask_source_region_code
                  AND cache.source_layer = ANY(:exclude_source_layers)
                  AND cache.geom_projected && tile.geom
                  AND ST_Intersects(cache.geom_projected, tile.geom)
            ),
            tile_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            CASE
                                WHEN :use_exclusion_mask
                                  AND exclusion_mask.geom IS NOT NULL
                                  AND NOT ST_IsEmpty(exclusion_mask.geom)
                                THEN ST_Difference(forest_mask.geom, exclusion_mask.geom)
                                ELSE forest_mask.geom
                            END
                        ),
                        3
                    )
                ) AS geom
                FROM forest_mask
                LEFT JOIN exclusion_mask ON :use_exclusion_mask
                WHERE forest_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(forest_mask.geom)
            ),
            clean_tile_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_Intersection(tile_mask.geom, canonical_region_mask.geom)),
                        3
                    )
                ) AS geom
                FROM tile_mask
                JOIN canonical_region_mask
                  ON canonical_region_mask.geom IS NOT NULL
                 AND NOT ST_IsEmpty(canonical_region_mask.geom)
                WHERE tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(tile_mask.geom)
            ),
            candidates AS (
                SELECT
                    square.geom AS square_geom,
                    square.i,
                    square.j
                FROM clean_tile_mask,
                     ST_SquareGrid(:grid_size_m, clean_tile_mask.geom) AS square
                WHERE clean_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(clean_tile_mask.geom)
                  AND square.geom && clean_tile_mask.geom
                  AND ST_Intersects(square.geom, clean_tile_mask.geom)
            ),
            clipped AS (
                SELECT
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(square_geom, clean_tile_mask.geom)),
                            3
                        )
                    ) AS geom,
                    i,
                    j
                FROM candidates, clean_tile_mask
            ),
            valid_clipped AS (
                SELECT geom, i, j
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            ),
            prepared AS (
                SELECT
                    (
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 1, 8) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 9, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 13, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 17, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 21, 12)
                    )::uuid AS uuid,
                    :region_id AS region_id,
                    {_grid_cell_geom_sql('valid_clipped.geom')},
                    {_grid_cell_area_ha_sql('valid_clipped.geom')},
                    :grid_version AS grid_version
                FROM valid_clipped
            ),
            valid_prepared AS (
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM prepared
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            changed AS (
                SELECT count(*) AS count
                FROM grid_cells cell
                JOIN valid_prepared candidate
                  ON candidate.uuid = cell.uuid
                 AND candidate.grid_version = cell.grid_version
                WHERE cell.region_id <> candidate.region_id
                   OR NOT ST_Equals(cell.geom, candidate.geom)
                   OR cell.area_ha <> candidate.area_ha
            ),
            inserted AS (
                INSERT INTO grid_cells (uuid, region_id, geom, area_ha, grid_version)
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM valid_prepared
                WHERE (SELECT count FROM changed) = 0
                ON CONFLICT (uuid, grid_version) DO NOTHING
                RETURNING 1
            )
            SELECT
                CASE
                    WHEN (SELECT count FROM changed) > 0 THEN -1
                    ELSE (SELECT count(*) FROM inserted)
                END AS inserted_cells
            """
        ),
        {
            "region_code": args.region_code,
            "region_id": tile["region_id"],
            "grid_version": tile["grid_version"],
            "grid_size_m": tile["grid_size_m"],
            "projected_srid": tile["projected_srid"],
            "min_x": tile["min_x"],
            "min_y": tile["min_y"],
            "max_x": tile["max_x"],
            "max_y": tile["max_y"],
            "mask_source_layers": list(getattr(args, "mask_source_layers", []) or []),
            "mask_intersect_source_layers": list(getattr(args, "mask_intersect_source_layers", []) or []),
            "mask_source_region_code": getattr(args, "mask_source_region_code", "") or args.region_code,
            "use_intersect_mask": bool(getattr(args, "mask_intersect_source_layers", [])),
            "use_exclusion_mask": bool(getattr(args, "exclude_source_layers", [])),
            "exclude_source_layers": list(getattr(args, "exclude_source_layers", []) or []),
        },
    ).scalar_one()


def _insert_tile_cells_from_region_mask_components(db, tile: dict[str, Any], args) -> int:
    return db.execute(
        text(
            f"""
            WITH tile AS (
                SELECT ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, :projected_srid) AS geom
            ),
            clean_tile_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_UnaryUnion(
                            ST_Collect(
                                ST_Intersection(component.geom_projected, tile.geom)
                            )
                        ),
                        3
                    )
                ) AS geom
                FROM stage3_region_mask_components component, tile
                WHERE component.region_id = :region_id
                  AND component.mask_version = :mask_version
                  AND component.projected_srid = :projected_srid
                  AND component.geom_projected && tile.geom
                  AND ST_Intersects(component.geom_projected, tile.geom)
            ),
            candidates AS (
                SELECT
                    square.geom AS square_geom,
                    square.i,
                    square.j
                FROM clean_tile_mask,
                     ST_SquareGrid(:grid_size_m, clean_tile_mask.geom) AS square
                WHERE clean_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(clean_tile_mask.geom)
                  AND square.geom && clean_tile_mask.geom
                  AND ST_Intersects(square.geom, clean_tile_mask.geom)
            ),
            clipped AS (
                SELECT
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(square_geom, clean_tile_mask.geom)),
                            3
                        )
                    ) AS geom,
                    i,
                    j
                FROM candidates, clean_tile_mask
            ),
            valid_clipped AS (
                SELECT geom, i, j
                FROM clipped
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            ),
            prepared AS (
                SELECT
                    (
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 1, 8) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 9, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 13, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 17, 4) || '-' ||
                        substr(md5(:region_code || ':' || :grid_version || ':' || i::text || ':' || j::text), 21, 12)
                    )::uuid AS uuid,
                    :region_id AS region_id,
                    {_grid_cell_geom_sql('valid_clipped.geom')},
                    {_grid_cell_area_ha_sql('valid_clipped.geom')},
                    :grid_version AS grid_version
                FROM valid_clipped
            ),
            valid_prepared AS (
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM prepared
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            changed AS (
                SELECT count(*) AS count
                FROM grid_cells cell
                JOIN valid_prepared candidate
                  ON candidate.uuid = cell.uuid
                 AND candidate.grid_version = cell.grid_version
                WHERE cell.region_id <> candidate.region_id
                   OR NOT ST_Equals(cell.geom, candidate.geom)
                   OR cell.area_ha <> candidate.area_ha
            ),
            inserted AS (
                INSERT INTO grid_cells (uuid, region_id, geom, area_ha, grid_version)
                SELECT uuid, region_id, geom, area_ha, grid_version
                FROM valid_prepared
                WHERE (SELECT count FROM changed) = 0
                ON CONFLICT (uuid, grid_version) DO NOTHING
                RETURNING 1
            )
            SELECT
                CASE
                    WHEN (SELECT count FROM changed) > 0 THEN -1
                    ELSE (SELECT count(*) FROM inserted)
                END AS inserted_cells
            """
        ),
        {
            "region_code": args.region_code,
            "region_id": tile["region_id"],
            "grid_version": tile["grid_version"],
            "grid_size_m": tile["grid_size_m"],
            "projected_srid": tile["projected_srid"],
            "min_x": tile["min_x"],
            "min_y": tile["min_y"],
            "max_x": tile["max_x"],
            "max_y": tile["max_y"],
            "mask_version": _region_mask_components_version(args),
        },
    ).scalar_one()


def _configure_forest_mask_grid_options(args) -> None:
    metadata = getattr(args, "forest_region_metadata", {}) or {}
    forest_mask = metadata.get("forest_mask") or {}
    args.mask_source_layers = _parse_source_layers(forest_mask.get("source_layers") or [])
    args.mask_intersect_source_layers = _parse_source_layers(forest_mask.get("intersect_source_layers") or [])
    args.mask_source_region_code = str(forest_mask.get("source_region_code") or args.region_code)
    if float(getattr(args, "min_mask_component_area_ha", 0) or 0) <= 0:
        args.min_mask_component_area_ha = clean_mask_min_component_area_ha(metadata)
    if args.mask_source_layers:
        validate_forest_mask_source_layers(args.mask_source_layers)
    if args.mask_intersect_source_layers:
        validate_forest_mask_source_layers(args.mask_intersect_source_layers)
    if _uses_source_mask_grid(args) and not getattr(args, "exclude_source_layers", []):
        args.exclude_source_layers = _parse_source_layers(forest_mask.get("difference_source_layers") or [])
        if args.exclude_source_layers:
            validate_auxiliary_nonforest_exclusion_source_layers(args.exclude_source_layers)


def _uses_source_mask_grid(args) -> bool:
    return bool(
        getattr(args, "mask_source_layers", [])
        and (getattr(args, "grid_from_source_mask", False) or getattr(args, "use_source_mask_cache", False))
    )


def _uses_source_mask_cache(args) -> bool:
    return bool(getattr(args, "use_source_mask_cache", False))


def _uses_region_mask_components(args) -> bool:
    return bool(getattr(args, "use_region_mask_components", False))


def _region_mask_components_version(args) -> str:
    metadata = getattr(args, "forest_region_metadata", {}) or {}
    forest_mask = metadata.get("forest_mask") or {}
    return str(forest_mask.get("mask_version") or args.region_code)


def _source_mask_cache_metadata(args) -> dict[str, Any]:
    if not _uses_source_mask_cache(args):
        return {}
    layers = _source_mask_cache_layers(args)
    return {
        "enabled": True,
        "source_region_code": getattr(args, "mask_source_region_code", "") or args.region_code,
        "source_layers": layers,
        "projected_srid": int(getattr(args, "projected_srid", 0) or 0),
        "purpose": "derived_public_mask_grid_generation_cache",
        "clean_mask_policy": _clean_mask_policy_metadata(args),
    }


def _clean_mask_policy_metadata(args) -> dict[str, Any]:
    min_area = float(getattr(args, "min_mask_component_area_ha", 0) or 0)
    if min_area <= 0:
        return {}
    return {
        "method": "component_area_filter_before_grid_insert",
        "min_component_area_ha": min_area,
        "applies_before_grid_insert": True,
    }


def _source_mask_cache_layers(args) -> list[str]:
    layers: list[str] = []
    for source_layers in (
        getattr(args, "mask_source_layers", []) or [],
        getattr(args, "mask_intersect_source_layers", []) or [],
        getattr(args, "exclude_source_layers", []) or [],
    ):
        for source_layer in source_layers:
            if source_layer not in layers:
                layers.append(source_layer)
    return layers


def _validate_source_mask_cache(db, args) -> None:
    if not _uses_source_mask_grid(args):
        raise RuntimeError("Source mask cache can only be used with a source-mask forest grid")
    source_layers = _source_mask_cache_layers(args)
    if not source_layers:
        raise RuntimeError("Source mask cache requested without source layers")
    rows = db.execute(
        text(
            """
            SELECT
                source_layer,
                count(*)::bigint AS components,
                count(*) FILTER (
                    WHERE geom_projected IS NULL
                       OR ST_IsEmpty(geom_projected)
                       OR NOT ST_IsValid(geom_projected)
                )::bigint AS bad_components
            FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
            GROUP BY source_layer
            """
        ),
        {
            "source_region_code": getattr(args, "mask_source_region_code", "") or args.region_code,
            "projected_srid": args.projected_srid,
            "source_layers": source_layers,
        },
    ).mappings().all()
    by_layer = {str(row["source_layer"]): dict(row) for row in rows}
    missing_layers = [source_layer for source_layer in source_layers if int(by_layer.get(source_layer, {}).get("components", 0) or 0) <= 0]
    bad_layers = [
        source_layer
        for source_layer in source_layers
        if int(by_layer.get(source_layer, {}).get("bad_components", 0) or 0) > 0
    ]
    if missing_layers:
        raise RuntimeError(f"Stage 3 source mask cache is missing layer(s): {', '.join(missing_layers)}")
    if bad_layers:
        raise RuntimeError(f"Stage 3 source mask cache has invalid component(s): {', '.join(bad_layers)}")


def _grid_mask_method(args) -> str:
    if _uses_region_mask_components(args):
        return "region_mask_components"
    return "per_tile_source_mask" if _uses_source_mask_grid(args) else "region_geometry"


def _mark_tile_completed(db, tile_id: int, inserted_cells: int) -> None:
    if inserted_cells < 0:
        raise RuntimeError("Existing grid cell geometry differs from generated tile")
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_tiles
            SET status = 'completed',
                inserted_cells = :inserted_cells,
                error = NULL,
                processed_at = now()
            WHERE id = :tile_id
            """
        ),
        {"tile_id": tile_id, "inserted_cells": inserted_cells},
    )


def _mark_tile_failed(db, tile_id: int, error: str) -> None:
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_tiles
            SET status = 'failed',
                attempt_count = attempt_count + 1,
                error = :error,
                processed_at = now()
            WHERE id = :tile_id
              AND status IN ('pending', 'failed')
            """
        ),
        {"tile_id": tile_id, "error": error},
    )


def _configure_tile_statement_timeout(db, args) -> None:
    timeout_ms = int(getattr(args, "statement_timeout_ms", 0) or 0)
    if timeout_ms <= 0:
        return
    db.execute(
        text("SELECT set_config('statement_timeout', :statement_timeout_ms, true)"),
        {"statement_timeout_ms": str(timeout_ms)},
    )


def _update_run_progress(db, run_id: int) -> None:
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs
            SET processed_tiles = progress.processed_tiles,
                inserted_cells = progress.inserted_cells,
                status = CASE
                    WHEN progress.failed_tiles > 0 THEN 'running'
                    WHEN progress.processed_tiles >= total_tiles THEN 'completed'
                    ELSE 'running'
                END,
                finished_at = CASE
                    WHEN progress.failed_tiles = 0 AND progress.processed_tiles >= total_tiles THEN now()
                    ELSE NULL
                END
            FROM (
                SELECT
                    count(*) FILTER (WHERE status IN ('completed', 'failed')) AS processed_tiles,
                    count(*) FILTER (WHERE status = 'failed') AS failed_tiles,
                    COALESCE(sum(inserted_cells) FILTER (WHERE status = 'completed'), 0) AS inserted_cells
                FROM stage3_grid_generation_tiles
                WHERE run_id = :run_id
            ) progress
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id},
    )


def _finish_run_if_complete(db, region_code: str, grid_version: str) -> None:
    db.execute(
        text(
            """
            UPDATE stage3_grid_generation_runs run
            SET status = CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM stage3_grid_generation_tiles tile
                        WHERE tile.run_id = run.id
                          AND tile.status = 'failed'
                    ) THEN 'failed'
                    ELSE 'completed'
                END,
                finished_at = now()
            FROM regions region
            WHERE region.id = run.region_id
              AND region.code = :region_code
              AND run.grid_version = :grid_version
              AND run.status = 'running'
              AND NOT EXISTS (
                  SELECT 1
                  FROM stage3_grid_generation_tiles tile
                  WHERE tile.run_id = run.id
                    AND (
                        tile.status = 'pending'
                        OR (
                            tile.status = 'failed'
                            AND tile.attempt_count < COALESCE((run.metadata->>'max_tile_attempts')::integer, 3)
                        )
                    )
              )
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    )


def _run_status(db, run_id: int) -> str:
    return str(db.execute(
        text("SELECT status FROM stage3_grid_generation_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).scalar_one())


def _mark_run_failed(region_code: str, grid_version: str, error: str) -> None:
    with SessionLocal() as db:
        db.execute(
            text(
                """
                UPDATE stage3_grid_generation_runs run
                SET status = 'failed',
                    finished_at = now(),
                    metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{error}',
                        to_jsonb(CAST(:error AS text)),
                        true
                    )
                FROM regions region
                WHERE region.id = run.region_id
                  AND region.code = :region_code
                  AND run.grid_version = :grid_version
                  AND run.status = 'running'
                """
            ),
            {
                "region_code": region_code,
                "grid_version": grid_version,
                "error": error,
            },
        )
        db.commit()


def _short_error(exc: Exception) -> str:
    message = str(exc).splitlines()
    detail = message[0] if message else "no details"
    return f"{type(exc).__name__}: {detail[:240]}"


def _parse_source_layers(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [part.strip() for part in value if part.strip()]
    return [part.strip() for part in value.split(",") if part.strip()]


if __name__ == "__main__":
    main()
