from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.forest_scope import forest_mask_source_layers, public_forest_mask_metadata_ready
from app.services.source_strategy import (
    SOURCE_LAYER_OSM_FOREST,
    SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
    SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
)


FOREST_COVERAGE_TOLERANCE_M2 = 1.0
SOURCE_MASK_SCALE_TOLERANCE_RATIO = 0.000005
SOURCE_MASK_SCALE_TOLERANCE_CAP_M2 = 200_000.0
TILE_SEAM_TOLERANCE_M = 2.0
MASK_EDGE_TOLERANCE_M = 1.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Check that Stage 3 grid cells are clipped to the forest mask")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", required=True)
    parser.add_argument("--expected-area-ha", type=float, default=1.0)
    parser.add_argument(
        "--tile-batch-size",
        type=int,
        default=250,
        help="Tile batch size for source-mask-cache validation.",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.tile_batch_size <= 0:
        raise ValueError("tile-batch-size must be greater than zero")

    with SessionLocal() as db:
        report = forest_clip_report(
            db,
            region_code=args.region_code,
            grid_version=args.grid_version,
            expected_area_ha=args.expected_area_ha,
            tile_batch_size=args.tile_batch_size,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


def forest_clip_report(
    db,
    *,
    region_code: str,
    grid_version: str,
    expected_area_ha: float = 1.0,
    tile_batch_size: int = 250,
) -> dict:
    if tile_batch_size <= 0:
        raise ValueError("tile_batch_size must be greater than zero")
    context = _load_clip_context(db, region_code=region_code, grid_version=grid_version)
    if _uses_source_mask_cache(context):
        return _source_mask_cache_forest_clip_report(
            db,
            context=context,
            region_code=region_code,
            grid_version=grid_version,
            expected_area_ha=expected_area_ha,
            tile_batch_size=tile_batch_size,
        )
    if _uses_region_mask_components(context):
        return _region_mask_components_forest_clip_report(
            db,
            context=context,
            region_code=region_code,
            grid_version=grid_version,
            expected_area_ha=expected_area_ha,
            tile_batch_size=tile_batch_size,
        )
    if _uses_source_mask_clip(context):
        return _source_mask_forest_clip_report(
            db,
            context=context,
            region_code=region_code,
            grid_version=grid_version,
            expected_area_ha=expected_area_ha,
        )
    return _region_geometry_forest_clip_report(
        db,
        region_code=region_code,
        grid_version=grid_version,
        expected_area_ha=expected_area_ha,
    )


def _load_clip_context(db, *, region_code: str, grid_version: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH region_scope AS (
                SELECT id, metadata
                FROM regions
                WHERE code = :region_code
            ),
            latest_grid_run AS (
                SELECT run.metadata, run.status, run.total_tiles, run.processed_tiles
                FROM stage3_grid_generation_runs run
                JOIN region_scope region ON region.id = run.region_id
                WHERE run.grid_version = :grid_version
                ORDER BY run.id DESC
                LIMIT 1
            )
            SELECT
                region_scope.id AS region_id,
                region_scope.metadata AS region_metadata,
                COALESCE(latest_grid_run.metadata, '{}'::jsonb) AS run_metadata,
                latest_grid_run.status AS grid_run_status,
                COALESCE(latest_grid_run.total_tiles, 0)::int AS total_tiles,
                COALESCE(latest_grid_run.processed_tiles, 0)::int AS processed_tiles
            FROM region_scope
            LEFT JOIN latest_grid_run ON true
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one()
    return {
        "region_id": int(row["region_id"]),
        "region_metadata": _json_dict(row["region_metadata"]),
        "run_metadata": _json_dict(row["run_metadata"]),
        "grid_run_status": row["grid_run_status"] or "",
        "total_tiles": int(row["total_tiles"] or 0),
        "processed_tiles": int(row["processed_tiles"] or 0),
    }


def _uses_source_mask_clip(context: dict[str, Any]) -> bool:
    run_metadata = context.get("run_metadata") or {}
    return (
        run_metadata.get("grid_mask_method") == "per_tile_source_mask"
        and bool(run_metadata.get("mask_source_layers") or [])
    )


def _uses_source_mask_cache(context: dict[str, Any]) -> bool:
    run_metadata = context.get("run_metadata") or {}
    source_mask_cache = run_metadata.get("source_mask_cache") or {}
    return (
        _uses_source_mask_clip(context)
        and bool(source_mask_cache.get("enabled"))
        and bool(source_mask_cache.get("source_region_code") or run_metadata.get("mask_source_region_code"))
    )


def _uses_region_mask_components(context: dict[str, Any]) -> bool:
    run_metadata = context.get("run_metadata") or {}
    return run_metadata.get("grid_mask_method") == "region_mask_components"


def _region_mask_components_forest_clip_report(
    db,
    *,
    context: dict[str, Any],
    region_code: str,
    grid_version: str,
    expected_area_ha: float = 1.0,
    tile_batch_size: int = 250,
) -> dict:
    metadata = _json_dict(context.get("region_metadata") or {})
    forest_mask = metadata.get("forest_mask") or {}
    mask_version = str(forest_mask.get("mask_version") or region_code)
    grid_run = _source_mask_cache_grid_run(
        db,
        region_code=region_code,
        grid_version=grid_version,
    )
    projected_srid = int(grid_run.get("projected_srid") or 0)
    if not projected_srid:
        raise RuntimeError("region-mask component validation requires projected_srid")

    cell_metrics = _source_mask_cache_cell_metrics(
        db,
        region_code=region_code,
        grid_version=grid_version,
        expected_area_ha=expected_area_ha,
    )
    component_summary = _region_mask_component_summary(
        db,
        region_id=int(grid_run["region_id"]),
        mask_version=mask_version,
        projected_srid=projected_srid,
    )
    run_metadata = context.get("run_metadata") or {}
    source_cache = forest_mask.get("source_mask_cache") or {}
    source_region_code = str(
        source_cache.get("source_region_code")
        or forest_mask.get("source_region_code")
        or run_metadata.get("mask_source_region_code")
        or region_code
    )
    exclusion_layers = sorted(
        set(
            list(forest_mask.get("difference_source_layers") or [])
            + list(run_metadata.get("exclude_source_layers") or [])
        )
    )
    cache_counts = _source_mask_cache_counts(
        db,
        source_region_code=source_region_code,
        projected_srid=projected_srid,
        source_layers=exclusion_layers,
    )
    cache_has_required_layers = bool(exclusion_layers) and all(
        cache_counts.get(layer, 0) > 0 for layer in exclusion_layers
    )
    exclusion_totals = {
        "measured_tiles": 0,
        "raw_tiles_intersect_nonforest_exclusion": 0,
        "interior_tiles_intersect_nonforest_exclusion": 0,
        "raw_overlap_m2": 0.0,
        "interior_overlap_m2": 0.0,
    }

    totals = {
        "batch_count": 0,
        "tile_count": 0,
        "completed_tiles": 0,
        "pending_tiles": 0,
        "running_tiles": 0,
        "failed_tiles": 0,
        "source_mask_tiles": 0,
        "tiles_outside_forest_mask": 0,
        "raw_tiles_outside_forest_mask": 0,
        "source_mask_area_m2": 0.0,
        "uncovered_forest_area_m2": 0.0,
        "outside_forest_mask_m2": 0.0,
        "raw_uncovered_forest_area_m2": 0.0,
        "raw_outside_forest_mask_m2": 0.0,
    }
    tile_offset = 0
    while tile_offset < int(grid_run["total_tiles"] or 0):
        batch = _region_mask_component_tile_batch(
            db,
            run_id=int(grid_run["run_id"]),
            region_id=int(grid_run["region_id"]),
            grid_version=grid_version,
            mask_version=mask_version,
            projected_srid=projected_srid,
            tile_limit=tile_batch_size,
            tile_offset=tile_offset,
        )
        totals["batch_count"] += 1
        for key in (
            "tile_count",
            "completed_tiles",
            "pending_tiles",
            "running_tiles",
            "failed_tiles",
            "source_mask_tiles",
            "tiles_outside_forest_mask",
            "raw_tiles_outside_forest_mask",
        ):
            totals[key] += int(batch.get(key) or 0)
        for key in (
            "source_mask_area_m2",
            "uncovered_forest_area_m2",
            "outside_forest_mask_m2",
            "raw_uncovered_forest_area_m2",
            "raw_outside_forest_mask_m2",
        ):
            totals[key] += float(batch.get(key) or 0)
        if cache_has_required_layers:
            exclusion_batch = _source_mask_cache_exclusion_tile_batch(
                db,
                run_id=int(grid_run["run_id"]),
                region_id=int(grid_run["region_id"]),
                grid_version=grid_version,
                projected_srid=projected_srid,
                source_region_code=source_region_code,
                exclusion_layers=exclusion_layers,
                tile_limit=tile_batch_size,
                tile_offset=tile_offset,
            )
            for key in exclusion_totals:
                # Missing measurement columns must never become measured zeroes.
                exclusion_totals[key] += exclusion_batch[key]
        tile_offset += tile_batch_size

    source_area_m2 = totals["source_mask_area_m2"]
    uncovered_m2 = totals["uncovered_forest_area_m2"]
    outside_m2 = totals["outside_forest_mask_m2"]
    rebuilt_mask_difference_ratio = (
        (uncovered_m2 + outside_m2) / source_area_m2
        if source_area_m2
        else 1.0
    )
    source_mask_tolerance_m2 = _source_mask_scale_tolerance_m2(source_area_m2)
    exclusion_measured = (
        cache_has_required_layers
        and exclusion_totals["measured_tiles"] == int(grid_run["total_tiles"] or 0)
        and exclusion_totals["measured_tiles"] > 0
    )
    all_tiles_completed = (
        str(grid_run["status"] or "") == "completed"
        and totals["pending_tiles"] == 0
        and totals["running_tiles"] == 0
        and totals["failed_tiles"] == 0
        and totals["tile_count"] == int(grid_run["total_tiles"] or 0)
    )
    checks = {
        "has_cells": int(cell_metrics["cell_count"] or 0) > 0,
        "valid_geometry": int(cell_metrics["invalid_cells"] or 0) == 0,
        "all_cells_inside_forest_mask": outside_m2 <= source_mask_tolerance_m2,
        "nonforest_exclusion_measured": exclusion_measured,
        "no_cells_intersect_nonforest_exclusion": (
            exclusion_measured
            and exclusion_totals["interior_overlap_m2"] <= source_mask_tolerance_m2
        ),
        "no_oversized_cells": int(cell_metrics["oversized_cells"] or 0) == 0,
        "polygonal_cell_geometry": int(cell_metrics["polygonal_cells"] or 0)
        == int(cell_metrics["cell_count"] or 0),
        "forest_mask_covered_by_clipped_grid": uncovered_m2 <= source_mask_tolerance_m2,
        "forest_mask_has_public_osm_clip": public_forest_mask_metadata_ready(metadata),
        "forest_mask_has_public_nonforest_difference": _public_nonforest_difference_metadata_ready(metadata),
        "forest_mask_has_clean_component_policy": _clean_mask_policy_ready(metadata),
        "rebuilt_ogm_osm_mask_available": int(component_summary["component_count"] or 0) > 0,
        "rebuilt_nonforest_difference_available": exclusion_measured,
        "region_matches_rebuilt_ogm_osm_mask": rebuilt_mask_difference_ratio <= 0.001,
        "all_source_mask_tiles_completed": all_tiles_completed,
        "source_mask_cache_has_required_layers": cache_has_required_layers,
        "final_mask_components_valid": int(component_summary["invalid_components"] or 0) == 0,
        "final_mask_components_above_min_area": int(component_summary["components_below_min_area"] or 0) == 0,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "cell_count": int(cell_metrics["cell_count"] or 0),
        "invalid_cells": int(cell_metrics["invalid_cells"] or 0),
        "cells_outside_forest_mask": None,
        "tiles_outside_forest_mask": int(totals["tiles_outside_forest_mask"]),
        "total_outside_forest_mask_m2": round(outside_m2, 6),
        "raw_cells_outside_forest_mask": None,
        "raw_tiles_outside_forest_mask": int(totals["raw_tiles_outside_forest_mask"]),
        "raw_total_outside_forest_mask_m2": round(totals["raw_outside_forest_mask_m2"], 6),
        "cells_intersect_nonforest_exclusion": None,
        "tiles_intersect_nonforest_exclusion": (
            int(exclusion_totals["raw_tiles_intersect_nonforest_exclusion"])
            if exclusion_measured else None
        ),
        "interior_tiles_intersect_nonforest_exclusion": (
            int(exclusion_totals["interior_tiles_intersect_nonforest_exclusion"])
            if exclusion_measured else None
        ),
        "total_nonforest_exclusion_overlap_m2": (
            round(exclusion_totals["raw_overlap_m2"], 6) if exclusion_measured else None
        ),
        "interior_total_nonforest_exclusion_overlap_m2": (
            round(exclusion_totals["interior_overlap_m2"], 6) if exclusion_measured else None
        ),
        "nonforest_exclusion_measurement": {
            "status": "measured" if exclusion_measured else "unmeasured",
            "method": "per_tile_source_mask_cache",
            "source_region_code": source_region_code,
            "source_layers": exclusion_layers,
            "projected_srid": projected_srid,
            "cache_counts": cache_counts,
            "measured_tiles": int(exclusion_totals["measured_tiles"]),
            "required_tiles": int(grid_run["total_tiles"] or 0),
            "edge_tolerance_m": MASK_EDGE_TOLERANCE_M,
            "gate_metric": "interior_total_nonforest_exclusion_overlap_m2",
            "gate_tolerance_m2": round(source_mask_tolerance_m2, 6),
            "independent_proof_required": not exclusion_measured,
            "note": (
                "When measured, raw overlap uses the cell and exclusion unions; interior overlap "
                "uses the exclusion union eroded by the existing mask edge tolerance. "
                "Overlap counts are tiles; affected-cell counts are unmeasured (null). "
                "Missing or incomplete cache evidence requires an independent exclusion "
                "proof before acceptance; this gate remains not ready."
            ),
        },
        "oversized_cells": int(cell_metrics["oversized_cells"] or 0),
        "clipped_edge_cells": int(cell_metrics["clipped_edge_cells"] or 0),
        "polygonal_cells": int(cell_metrics["polygonal_cells"] or 0),
        "rebuilt_mask_difference_ratio": round(rebuilt_mask_difference_ratio, 6),
        "forest_mask_coverage_ratio": round(
            1 - (uncovered_m2 / source_area_m2) if source_area_m2 else 0,
            5,
        ),
        "forest_mask_area_ha": round(source_area_m2 / 10000.0, 4),
        "active_grid_area_ha": round(float(cell_metrics["active_grid_area_ha"] or 0), 4),
        "uncovered_forest_area_m2": round(uncovered_m2, 6),
        "raw_uncovered_forest_area_m2": round(totals["raw_uncovered_forest_area_m2"], 6),
        "uncovered_forest_area_ha": round(uncovered_m2 / 10000.0, 4),
        "completed_tiles": int(totals["completed_tiles"]),
        "pending_tiles": int(totals["pending_tiles"]),
        "running_tiles": int(totals["running_tiles"]),
        "failed_tiles": int(totals["failed_tiles"]),
        "source_mask_cache_used": cache_has_required_layers,
        "region_mask_components_used": True,
        "region_mask_components": {
            "mask_version": mask_version,
            "projected_srid": projected_srid,
            "component_count": int(component_summary["component_count"] or 0),
            "min_component_area_ha": float(component_summary["min_component_area_ha"] or 0),
            "components_below_min_area": int(component_summary["components_below_min_area"] or 0),
            "invalid_components": int(component_summary["invalid_components"] or 0),
            "tile_batch_size": tile_batch_size,
            "batch_count": int(totals["batch_count"]),
        },
        "forest_coverage_tolerance_m2": FOREST_COVERAGE_TOLERANCE_M2,
        "tile_seam_tolerance_m": TILE_SEAM_TOLERANCE_M,
        "mask_edge_tolerance_m": MASK_EDGE_TOLERANCE_M,
        "source_mask_scale_tolerance_m2": round(source_mask_tolerance_m2, 6),
        "source_mask_scale_tolerance_ratio": SOURCE_MASK_SCALE_TOLERANCE_RATIO,
        "source_mask_scale_tolerance_cap_m2": SOURCE_MASK_SCALE_TOLERANCE_CAP_M2,
        "coverage_note": (
            "Stage 3 stores clipped forest geometry for each 100 m grid index. "
            "Final component-mask runs are checked per tile against "
            "stage3_region_mask_components to avoid a national-scale union geometry. "
            "Outside-mask counts are tiles; corresponding cell counts are unmeasured (null)."
        ),
        "forest_mask_source": forest_mask.get("source"),
        "forest_mask_method": forest_mask.get("method"),
        "clean_mask_policy": forest_mask.get("clean_mask_policy") or {},
        "checks": checks,
    }


def _region_mask_component_summary(
    db,
    *,
    region_id: int,
    mask_version: str,
    projected_srid: int,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
                count(*)::bigint AS component_count,
                COALESCE(min(area_ha), 0)::float AS min_component_area_ha,
                count(*) FILTER (
                    WHERE (
                        CASE
                            WHEN metadata ? 'connected_component_area_ha'
                            THEN NULLIF(metadata->>'connected_component_area_ha', '')::float
                            ELSE area_ha::float
                        END
                    ) < 1
                )::bigint AS components_below_min_area,
                count(*) FILTER (
                    WHERE geom_projected IS NULL
                       OR ST_IsEmpty(geom_projected)
                       OR NOT ST_IsValid(geom_projected)
                )::bigint AS invalid_components
            FROM stage3_region_mask_components
            WHERE region_id = :region_id
              AND mask_version = :mask_version
              AND projected_srid = :projected_srid
            """
        ),
        {
            "region_id": region_id,
            "mask_version": mask_version,
            "projected_srid": projected_srid,
        },
    ).mappings().one()
    return dict(row)


def _region_mask_component_tile_batch(
    db,
    *,
    run_id: int,
    region_id: int,
    grid_version: str,
    mask_version: str,
    projected_srid: int,
    tile_limit: int,
    tile_offset: int,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH selected_tiles AS (
                SELECT
                    tile.id,
                    tile.status,
                    ST_MakeEnvelope(
                        tile.min_x,
                        tile.min_y,
                        tile.max_x,
                        tile.max_y,
                        :projected_srid
                    ) AS geom
                FROM stage3_grid_generation_tiles tile
                WHERE tile.run_id = :run_id
                ORDER BY tile.id
                LIMIT :tile_limit
                OFFSET :tile_offset
            ),
            tile_geoms AS (
                SELECT
                    selected_tiles.*,
                    ST_Transform(selected_tiles.geom, 4326) AS geom_4326
                FROM selected_tiles
            ),
            tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(component.geom_projected, tile.geom)
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                JOIN stage3_region_mask_components component
                  ON component.region_id = :region_id
                 AND component.mask_version = :mask_version
                 AND component.projected_srid = :projected_srid
                 AND component.geom_projected && tile.geom
                 AND ST_Intersects(component.geom_projected, tile.geom)
                GROUP BY tile.id
            ),
            tile_cells AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(
                                            ST_MakeValid(
                                                ST_Transform(ST_MakeValid(cell.geom), :projected_srid)
                                            ),
                                            tile.geom
                                        )
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                JOIN grid_cells cell
                 ON cell.region_id = :region_id
                 AND cell.grid_version = :grid_version
                 AND cell.geom && tile.geom_4326
                 AND ST_Covers(tile.geom, ST_Transform(ST_PointOnSurface(cell.geom), :projected_srid))
                GROUP BY tile.id
            ),
            empty_geom AS (
                SELECT ST_GeomFromText('POLYGON EMPTY', :projected_srid) AS geom
            ),
            tile_seams AS (
                SELECT
                    tile_mask.id,
                    ST_UnaryUnion(ST_Collect(
                        ST_Buffer(ST_Boundary(tile_geoms.geom), :tile_seam_tolerance_m),
                        ST_Buffer(ST_Boundary(tile_mask.geom), :mask_edge_tolerance_m)
                    )) AS geom
                FROM tile_mask
                JOIN tile_geoms ON tile_geoms.id = tile_mask.id
            ),
            tile_differences AS (
                SELECT
                    tile_mask.id,
                    ST_Area(tile_mask.geom)::float AS source_mask_area_m2,
                    ST_Area(
                        ST_Difference(
                            ST_MakeValid(tile_mask.geom),
                            ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom))
                        )
                    )::float AS raw_uncovered_forest_area_m2,
                    ST_Area(
                        ST_Difference(
                            ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom)),
                            ST_MakeValid(tile_mask.geom)
                        )
                    )::float AS raw_outside_forest_mask_m2,
                    ST_Area(
                        ST_Difference(
                            ST_Difference(
                                ST_MakeValid(tile_mask.geom),
                                ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom))
                            ),
                            tile_seams.geom
                        )
                    )::float AS uncovered_forest_area_m2,
                    ST_Area(
                        ST_Difference(
                            ST_Difference(
                                ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom)),
                                ST_MakeValid(tile_mask.geom)
                            ),
                            tile_seams.geom
                        )
                    )::float AS outside_forest_mask_m2
                FROM tile_mask
                CROSS JOIN empty_geom
                JOIN tile_seams ON tile_seams.id = tile_mask.id
                LEFT JOIN tile_cells ON tile_cells.id = tile_mask.id
                WHERE tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(tile_mask.geom)
            )
            SELECT
                count(tile_geoms.id)::int AS tile_count,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'completed')::int AS completed_tiles,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'pending')::int AS pending_tiles,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'running')::int AS running_tiles,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'failed')::int AS failed_tiles,
                count(tile_mask.id)::int AS source_mask_tiles,
                count(tile_differences.id) FILTER (
                    WHERE tile_differences.outside_forest_mask_m2 > 0.01
                )::int AS tiles_outside_forest_mask,
                count(tile_differences.id) FILTER (
                    WHERE tile_differences.raw_outside_forest_mask_m2 > 0.01
                )::int AS raw_tiles_outside_forest_mask,
                COALESCE(sum(tile_differences.source_mask_area_m2), 0)::float AS source_mask_area_m2,
                COALESCE(sum(tile_differences.uncovered_forest_area_m2), 0)::float
                    AS uncovered_forest_area_m2,
                COALESCE(sum(tile_differences.outside_forest_mask_m2), 0)::float
                    AS outside_forest_mask_m2,
                COALESCE(sum(tile_differences.raw_uncovered_forest_area_m2), 0)::float
                    AS raw_uncovered_forest_area_m2,
                COALESCE(sum(tile_differences.raw_outside_forest_mask_m2), 0)::float
                    AS raw_outside_forest_mask_m2
            FROM tile_geoms
            LEFT JOIN tile_mask ON tile_mask.id = tile_geoms.id
            LEFT JOIN tile_differences ON tile_differences.id = tile_geoms.id
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "grid_version": grid_version,
            "mask_version": mask_version,
            "projected_srid": projected_srid,
            "tile_limit": tile_limit,
            "tile_offset": tile_offset,
            "tile_seam_tolerance_m": TILE_SEAM_TOLERANCE_M,
            "mask_edge_tolerance_m": MASK_EDGE_TOLERANCE_M,
        },
    ).mappings().one()
    return dict(row)


def _region_geometry_forest_clip_report(
    db,
    *,
    region_code: str,
    grid_version: str,
    expected_area_ha: float = 1.0,
) -> dict:
    row = db.execute(
        text(
            """
            WITH region AS (
                SELECT id, geom, metadata
                FROM regions
                WHERE code = :region_code
            ),
            source_scope AS (
                SELECT COALESCE(metadata->'forest_mask'->>'source_region_code', :region_code) AS source_region_code
                FROM region
            ),
            layer_a_boundary AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_UnaryUnion(ST_Collect(parcel.geom))),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel
                CROSS JOIN source_scope
                WHERE parcel.is_current
                  AND parcel.source_layer = ANY(:layer_a_source_layers)
                  AND parcel.attributes->>'region_code' = source_scope.source_region_code
            ),
            layer_a_forest_class AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_UnaryUnion(ST_Collect(parcel.geom))),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel
                CROSS JOIN source_scope
                WHERE parcel.is_current
                  AND parcel.source_layer = 'ogm_yayin_mescere_wms_svg'
                  AND parcel.attributes->>'region_code' = source_scope.source_region_code
                  AND upper(COALESCE(
                      parcel.forest_type,
                      parcel.attributes->'source_attributes'->>'MESCERE',
                      ''
                  )) = 'F'
            ),
            osm_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_UnaryUnion(ST_Collect(parcel.geom))),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel
                CROSS JOIN source_scope
                WHERE parcel.is_current
                  AND parcel.source_layer = :osm_forest_source_layer
                  AND parcel.attributes->>'region_code' = source_scope.source_region_code
            ),
            difference_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_UnaryUnion(ST_Collect(parcel.geom))),
                        3
                    )
                ) AS geom
                FROM ogm_parcels parcel
                CROSS JOIN source_scope
                WHERE parcel.is_current
                  AND parcel.source_layer = ANY(:nonforest_source_layers)
                  AND parcel.attributes->>'region_code' = source_scope.source_region_code
            ),
            forest_candidates AS (
                SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM (
                    SELECT geom
                    FROM layer_a_forest_class
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                    UNION ALL
                    SELECT ST_Intersection(layer_a_boundary.geom, osm_mask.geom) AS geom
                    FROM layer_a_boundary, osm_mask
                    WHERE layer_a_boundary.geom IS NOT NULL
                      AND osm_mask.geom IS NOT NULL
                      AND NOT ST_IsEmpty(layer_a_boundary.geom)
                      AND NOT ST_IsEmpty(osm_mask.geom)
                ) candidate
                WHERE candidate.geom IS NOT NULL
                  AND NOT ST_IsEmpty(candidate.geom)
            ),
            rebuilt_seed_mask AS (
                SELECT
                    CASE
                        WHEN region.metadata->'forest_mask'->>'source' IN (
                            'public_osm_forest_mask',
                            'public_osm_forest_mask_with_nonforest_difference'
                        )
                        THEN osm_mask.geom
                        WHEN layer_a_boundary.geom IS NOT NULL
                          AND forest_candidates.geom IS NOT NULL
                          AND NOT ST_IsEmpty(layer_a_boundary.geom)
                          AND NOT ST_IsEmpty(forest_candidates.geom)
                        THEN ST_Intersection(layer_a_boundary.geom, forest_candidates.geom)
                    END AS geom
                FROM region, layer_a_boundary, forest_candidates, osm_mask
            ),
            rebuilt_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            CASE
                                WHEN difference_mask.geom IS NOT NULL
                                  AND NOT ST_IsEmpty(difference_mask.geom)
                                THEN ST_Difference(rebuilt_seed_mask.geom, difference_mask.geom)
                                ELSE rebuilt_seed_mask.geom
                            END
                        ),
                        3
                    )
                ) AS geom
                FROM rebuilt_seed_mask, difference_mask
                WHERE rebuilt_seed_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(rebuilt_seed_mask.geom)
            ),
            cells AS (
                SELECT cell.id, cell.geom, cell.area_ha
                FROM grid_cells cell
                JOIN region ON region.id = cell.region_id
                WHERE cell.grid_version = :grid_version
            ),
            cell_union AS (
                SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM cells
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            cell_area AS (
                SELECT COALESCE(sum(ST_Area(geom::geography)), 0)::float AS active_grid_area_m2
                FROM cells
            ),
            forest_coverage AS (
                SELECT
                    COALESCE(ST_Area(region.geom::geography), 0)::float AS forest_mask_area_m2,
                    COALESCE(
                        CASE
                            WHEN cell_union.geom IS NULL OR ST_IsEmpty(cell_union.geom)
                            THEN ST_Area(region.geom::geography)
                            ELSE ST_Area(ST_Difference(region.geom, cell_union.geom)::geography)
                        END,
                        ST_Area(region.geom::geography),
                        0
                    )::float AS uncovered_forest_area_m2
                FROM region
                LEFT JOIN cell_union ON true
            )
            SELECT
                (SELECT count(*) FROM cells)::int AS cell_count,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE geom IS NULL
                       OR ST_IsEmpty(geom)
                       OR NOT ST_IsValid(geom)
                )::int AS invalid_cells,
                (
                    SELECT count(*)
                    FROM cells, region
                    WHERE ST_Area(ST_Difference(cells.geom, region.geom)::geography) > 0.01
                )::int AS cells_outside_forest_mask,
                (
                    SELECT COALESCE(sum(ST_Area(ST_Difference(cells.geom, region.geom)::geography)), 0)
                    FROM cells, region
                )::float AS total_outside_forest_mask_m2,
                (
                    SELECT count(*)
                    FROM cells, difference_mask
                    WHERE difference_mask.geom IS NOT NULL
                      AND NOT ST_IsEmpty(difference_mask.geom)
                      AND cells.geom && difference_mask.geom
                      AND ST_Area(ST_Intersection(cells.geom, difference_mask.geom)::geography) > 0.01
                )::int AS cells_intersect_nonforest_exclusion,
                (
                    SELECT COALESCE(sum(ST_Area(ST_Intersection(cells.geom, difference_mask.geom)::geography)), 0)
                    FROM cells, difference_mask
                    WHERE difference_mask.geom IS NOT NULL
                      AND NOT ST_IsEmpty(difference_mask.geom)
                      AND cells.geom && difference_mask.geom
                )::float AS total_nonforest_exclusion_overlap_m2,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE ST_Area(geom::geography) / 10000.0 > (:expected_area_ha * 1.01)
                )::int AS oversized_cells,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE ST_Area(geom::geography) / 10000.0 < (:expected_area_ha * 0.99)
                )::int AS clipped_edge_cells,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
                )::int AS polygonal_cells,
                (
                    SELECT COALESCE(
                        ST_Area(ST_SymDifference(region.geom, rebuilt_mask.geom)::geography)
                        / NULLIF(ST_Area(region.geom::geography), 0),
                        1
                    )
                    FROM region
                    LEFT JOIN rebuilt_mask ON true
                )::float AS rebuilt_mask_difference_ratio,
                (
                    SELECT count(*) > 0
                    FROM rebuilt_mask
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                ) AS rebuilt_mask_available,
                (
                    SELECT count(*) > 0
                    FROM difference_mask
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                ) AS rebuilt_difference_mask_available,
                COALESCE(
                    1 - (
                        forest_coverage.uncovered_forest_area_m2
                        / NULLIF(forest_coverage.forest_mask_area_m2, 0)
                    ),
                    0
                )::float AS forest_mask_coverage_ratio,
                COALESCE(forest_coverage.forest_mask_area_m2 / 10000.0, 0)::float AS forest_mask_area_ha,
                COALESCE(cell_area.active_grid_area_m2 / 10000.0, 0)::float AS active_grid_area_ha,
                COALESCE(forest_coverage.uncovered_forest_area_m2, 0)::float AS uncovered_forest_area_m2,
                COALESCE(forest_coverage.uncovered_forest_area_m2 / 10000.0, 0)::float AS uncovered_forest_area_ha,
                (SELECT metadata FROM region) AS region_metadata
            FROM region
            CROSS JOIN cell_area
            CROSS JOIN forest_coverage
            """
        ),
        {
            "region_code": region_code,
            "grid_version": grid_version,
            "expected_area_ha": expected_area_ha,
            "layer_a_source_layers": forest_mask_source_layers(),
            "osm_forest_source_layer": SOURCE_LAYER_OSM_FOREST,
            "nonforest_source_layers": [
                SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
                SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
            ],
        },
    ).mappings().one()
    metadata = row["region_metadata"] or {}
    rebuilt_mask_difference_ratio = (
        1.0 if row["rebuilt_mask_difference_ratio"] is None else float(row["rebuilt_mask_difference_ratio"])
    )
    checks = {
        "has_cells": int(row["cell_count"] or 0) > 0,
        "valid_geometry": int(row["invalid_cells"] or 0) == 0,
        "all_cells_inside_forest_mask": int(row["cells_outside_forest_mask"] or 0) == 0,
        "no_cells_intersect_nonforest_exclusion": int(row["cells_intersect_nonforest_exclusion"] or 0) == 0,
        "no_oversized_cells": int(row["oversized_cells"] or 0) == 0,
        "polygonal_cell_geometry": int(row["polygonal_cells"] or 0) == int(row["cell_count"] or 0),
        "forest_mask_covered_by_clipped_grid": _float_value(row, "uncovered_forest_area_m2") <= FOREST_COVERAGE_TOLERANCE_M2,
        "forest_mask_has_public_osm_clip": public_forest_mask_metadata_ready(metadata),
        "forest_mask_has_public_nonforest_difference": _public_nonforest_difference_metadata_ready(metadata),
        "forest_mask_has_clean_component_policy": _clean_mask_policy_ready(metadata),
        "rebuilt_ogm_osm_mask_available": bool(row["rebuilt_mask_available"]),
        "rebuilt_nonforest_difference_available": bool(row["rebuilt_difference_mask_available"]),
        "region_matches_rebuilt_ogm_osm_mask": rebuilt_mask_difference_ratio <= 0.001,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "cell_count": int(row["cell_count"] or 0),
        "invalid_cells": int(row["invalid_cells"] or 0),
        "cells_outside_forest_mask": int(row["cells_outside_forest_mask"] or 0),
        "total_outside_forest_mask_m2": round(_float_value(row, "total_outside_forest_mask_m2"), 6),
        "cells_intersect_nonforest_exclusion": int(row["cells_intersect_nonforest_exclusion"] or 0),
        "total_nonforest_exclusion_overlap_m2": round(_float_value(row, "total_nonforest_exclusion_overlap_m2"), 6),
        "oversized_cells": int(row["oversized_cells"] or 0),
        "clipped_edge_cells": int(row["clipped_edge_cells"] or 0),
        "polygonal_cells": int(row["polygonal_cells"] or 0),
        "rebuilt_mask_difference_ratio": round(rebuilt_mask_difference_ratio, 6),
        "forest_mask_coverage_ratio": round(float(row["forest_mask_coverage_ratio"] or 0), 5),
        "forest_mask_area_ha": round(_float_value(row, "forest_mask_area_ha"), 4),
        "active_grid_area_ha": round(_float_value(row, "active_grid_area_ha"), 4),
        "uncovered_forest_area_m2": round(_float_value(row, "uncovered_forest_area_m2"), 6),
        "uncovered_forest_area_ha": round(_float_value(row, "uncovered_forest_area_ha"), 4),
        "forest_coverage_tolerance_m2": FOREST_COVERAGE_TOLERANCE_M2,
        "coverage_note": (
            "Stage 3 stores the clipped forest geometry for each 100 m grid index. "
            "Edge assets may be smaller than 1 ha when the forest mask cuts through the square."
        ),
        "forest_mask_source": (metadata.get("forest_mask") or {}).get("source"),
        "forest_mask_method": (metadata.get("forest_mask") or {}).get("method"),
        "clean_mask_policy": (metadata.get("forest_mask") or {}).get("clean_mask_policy") or {},
        "checks": checks,
    }


def _source_mask_cache_forest_clip_report(
    db,
    *,
    context: dict[str, Any],
    region_code: str,
    grid_version: str,
    expected_area_ha: float = 1.0,
    tile_batch_size: int = 250,
) -> dict:
    run_metadata = context.get("run_metadata") or {}
    source_mask_cache = run_metadata.get("source_mask_cache") or {}
    source_region_code = (
        source_mask_cache.get("source_region_code")
        or run_metadata.get("mask_source_region_code")
        or region_code
    )
    projected_srid = int(source_mask_cache.get("projected_srid") or 0)
    source_layers = list(run_metadata.get("mask_source_layers") or [])
    intersect_layers = list(run_metadata.get("mask_intersect_source_layers") or [])
    exclusion_layers = list(run_metadata.get("exclude_source_layers") or [])
    if not projected_srid:
        raise RuntimeError("source-mask cache validation requires projected_srid metadata")
    if not source_layers:
        raise RuntimeError("source-mask cache validation requires mask_source_layers metadata")

    metadata = _json_dict(context.get("region_metadata") or {})
    grid_run = _source_mask_cache_grid_run(
        db,
        region_code=region_code,
        grid_version=grid_version,
    )
    cell_metrics = _source_mask_cache_cell_metrics(
        db,
        region_code=region_code,
        grid_version=grid_version,
        expected_area_ha=expected_area_ha,
    )
    cache_counts = _source_mask_cache_counts(
        db,
        source_region_code=source_region_code,
        projected_srid=projected_srid,
        source_layers=source_layers + intersect_layers + exclusion_layers,
    )

    totals = {
        "batch_count": 0,
        "tile_count": 0,
        "completed_tiles": 0,
        "pending_tiles": 0,
        "running_tiles": 0,
        "failed_tiles": 0,
        "source_mask_tiles": 0,
        "exclusion_mask_tiles": 0,
        "tiles_outside_forest_mask": 0,
        "tiles_intersect_nonforest_exclusion": 0,
        "source_mask_area_m2": 0.0,
        "uncovered_forest_area_m2": 0.0,
        "outside_forest_mask_m2": 0.0,
        "nonforest_exclusion_overlap_m2": 0.0,
    }
    tile_offset = 0
    while tile_offset < int(grid_run["total_tiles"] or 0):
        batch = _source_mask_cache_tile_batch(
            db,
            run_id=int(grid_run["run_id"]),
            region_id=int(grid_run["region_id"]),
            grid_version=grid_version,
            projected_srid=projected_srid,
            source_region_code=source_region_code,
            source_layers=source_layers,
            intersect_layers=intersect_layers,
            exclusion_layers=exclusion_layers,
            tile_limit=tile_batch_size,
            tile_offset=tile_offset,
        )
        totals["batch_count"] += 1
        for key in (
            "tile_count",
            "completed_tiles",
            "pending_tiles",
            "running_tiles",
            "failed_tiles",
            "source_mask_tiles",
            "exclusion_mask_tiles",
            "tiles_outside_forest_mask",
            "tiles_intersect_nonforest_exclusion",
        ):
            totals[key] += int(batch.get(key) or 0)
        for key in (
            "source_mask_area_m2",
            "uncovered_forest_area_m2",
            "outside_forest_mask_m2",
            "nonforest_exclusion_overlap_m2",
        ):
            totals[key] += float(batch.get(key) or 0)
        tile_offset += tile_batch_size

    source_area_m2 = totals["source_mask_area_m2"]
    uncovered_m2 = totals["uncovered_forest_area_m2"]
    outside_m2 = totals["outside_forest_mask_m2"]
    nonforest_overlap_m2 = totals["nonforest_exclusion_overlap_m2"]
    rebuilt_mask_difference_ratio = (
        (uncovered_m2 + outside_m2) / source_area_m2
        if source_area_m2
        else 1.0
    )
    source_mask_tolerance_m2 = _source_mask_scale_tolerance_m2(source_area_m2)
    all_tiles_completed = (
        str(grid_run["status"] or "") == "completed"
        and totals["pending_tiles"] == 0
        and totals["running_tiles"] == 0
        and totals["failed_tiles"] == 0
        and totals["tile_count"] == int(grid_run["total_tiles"] or 0)
    )
    required_cache_layers = source_layers + intersect_layers + exclusion_layers
    cache_has_required_layers = all(cache_counts.get(source_layer, 0) > 0 for source_layer in required_cache_layers)
    checks = {
        "has_cells": int(cell_metrics["cell_count"] or 0) > 0,
        "valid_geometry": int(cell_metrics["invalid_cells"] or 0) == 0,
        "all_cells_inside_forest_mask": outside_m2 <= source_mask_tolerance_m2,
        "no_cells_intersect_nonforest_exclusion": nonforest_overlap_m2 <= source_mask_tolerance_m2,
        "no_oversized_cells": int(cell_metrics["oversized_cells"] or 0) == 0,
        "polygonal_cell_geometry": int(cell_metrics["polygonal_cells"] or 0)
        == int(cell_metrics["cell_count"] or 0),
        "forest_mask_covered_by_clipped_grid": uncovered_m2 <= source_mask_tolerance_m2,
        "forest_mask_has_public_osm_clip": public_forest_mask_metadata_ready(metadata),
        "forest_mask_has_public_nonforest_difference": _public_nonforest_difference_metadata_ready(metadata),
        "forest_mask_has_clean_component_policy": _clean_mask_policy_ready(metadata),
        "rebuilt_ogm_osm_mask_available": totals["source_mask_tiles"] > 0,
        "rebuilt_nonforest_difference_available": not exclusion_layers or totals["exclusion_mask_tiles"] > 0,
        "region_matches_rebuilt_ogm_osm_mask": rebuilt_mask_difference_ratio <= 0.001,
        "all_source_mask_tiles_completed": all_tiles_completed,
        "source_mask_cache_has_required_layers": cache_has_required_layers,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "cell_count": int(cell_metrics["cell_count"] or 0),
        "invalid_cells": int(cell_metrics["invalid_cells"] or 0),
        "cells_outside_forest_mask": int(totals["tiles_outside_forest_mask"]),
        "total_outside_forest_mask_m2": round(outside_m2, 6),
        "cells_intersect_nonforest_exclusion": int(totals["tiles_intersect_nonforest_exclusion"]),
        "total_nonforest_exclusion_overlap_m2": round(nonforest_overlap_m2, 6),
        "oversized_cells": int(cell_metrics["oversized_cells"] or 0),
        "clipped_edge_cells": int(cell_metrics["clipped_edge_cells"] or 0),
        "polygonal_cells": int(cell_metrics["polygonal_cells"] or 0),
        "rebuilt_mask_difference_ratio": round(rebuilt_mask_difference_ratio, 6),
        "forest_mask_coverage_ratio": round(
            1 - (uncovered_m2 / source_area_m2) if source_area_m2 else 0,
            5,
        ),
        "forest_mask_area_ha": round(source_area_m2 / 10000.0, 4),
        "active_grid_area_ha": round(float(cell_metrics["active_grid_area_ha"] or 0), 4),
        "uncovered_forest_area_m2": round(uncovered_m2, 6),
        "uncovered_forest_area_ha": round(uncovered_m2 / 10000.0, 4),
        "completed_tiles": int(totals["completed_tiles"]),
        "pending_tiles": int(totals["pending_tiles"]),
        "running_tiles": int(totals["running_tiles"]),
        "failed_tiles": int(totals["failed_tiles"]),
        "source_mask_cache_used": True,
        "source_mask_cache": {
            "source_region_code": source_region_code,
            "projected_srid": projected_srid,
            "source_layers": source_layers,
            "intersect_source_layers": intersect_layers,
            "difference_source_layers": exclusion_layers,
            "cache_counts": cache_counts,
            "tile_batch_size": tile_batch_size,
            "batch_count": int(totals["batch_count"]),
        },
        "forest_coverage_tolerance_m2": FOREST_COVERAGE_TOLERANCE_M2,
        "source_mask_scale_tolerance_m2": round(source_mask_tolerance_m2, 6),
        "source_mask_scale_tolerance_ratio": SOURCE_MASK_SCALE_TOLERANCE_RATIO,
        "source_mask_scale_tolerance_cap_m2": SOURCE_MASK_SCALE_TOLERANCE_CAP_M2,
        "coverage_note": (
            "Stage 3 stores the clipped forest geometry for each 100 m grid index. "
            "Full public source-mask runs are checked per tile against the derived "
            "public source-mask cache to avoid a national-scale union geometry. "
            "The cache path keeps a 1 m² floor and a strict ppm-level scale tolerance "
            "for independent tile-mask reconstruction over national public geometries."
        ),
        "forest_mask_source": (metadata.get("forest_mask") or {}).get("source"),
        "forest_mask_method": (metadata.get("forest_mask") or {}).get("method"),
        "clean_mask_policy": (metadata.get("forest_mask") or {}).get("clean_mask_policy") or {},
        "checks": checks,
    }


def _source_mask_cache_grid_run(db, *, region_code: str, grid_version: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
                run.id AS run_id,
                run.region_id,
                run.projected_srid,
                run.status,
                run.total_tiles,
                run.processed_tiles
            FROM stage3_grid_generation_runs run
            JOIN regions region ON region.id = run.region_id
            WHERE region.code = :region_code
              AND run.grid_version = :grid_version
            ORDER BY run.id DESC
            LIMIT 1
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one()
    return dict(row)


def _source_mask_cache_cell_metrics(
    db,
    *,
    region_code: str,
    grid_version: str,
    expected_area_ha: float,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH region AS (
                SELECT id
                FROM regions
                WHERE code = :region_code
            ),
            cells AS (
                SELECT cell.id, cell.geom, cell.area_ha
                FROM grid_cells cell
                JOIN region ON region.id = cell.region_id
                WHERE cell.grid_version = :grid_version
            )
            SELECT
                count(*)::bigint AS cell_count,
                count(*) FILTER (
                    WHERE geom IS NULL
                       OR ST_IsEmpty(geom)
                       OR NOT ST_IsValid(geom)
                )::bigint AS invalid_cells,
                count(*) FILTER (
                    WHERE COALESCE(area_ha, 0) > (:expected_area_ha * 1.01)
                )::bigint AS oversized_cells,
                count(*) FILTER (
                    WHERE COALESCE(area_ha, 0) < (:expected_area_ha * 0.99)
                )::bigint AS clipped_edge_cells,
                count(*) FILTER (
                    WHERE GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
                )::bigint AS polygonal_cells,
                COALESCE(sum(area_ha), 0)::float AS active_grid_area_ha
            FROM cells
            """
        ),
        {
            "region_code": region_code,
            "grid_version": grid_version,
            "expected_area_ha": expected_area_ha,
        },
    ).mappings().one()
    return dict(row)


def _source_mask_cache_counts(
    db,
    *,
    source_region_code: str,
    projected_srid: int,
    source_layers: list[str],
) -> dict[str, int]:
    unique_layers = sorted(set(source_layers))
    if not unique_layers:
        return {}
    rows = db.execute(
        text(
            """
            SELECT source_layer, count(*)::bigint AS count
            FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(CAST(:source_layers AS text[]))
            GROUP BY source_layer
            """
        ),
        {
            "source_region_code": source_region_code,
            "projected_srid": projected_srid,
            "source_layers": unique_layers,
        },
    ).mappings()
    counts = {source_layer: 0 for source_layer in unique_layers}
    counts.update({str(row["source_layer"]): int(row["count"] or 0) for row in rows})
    return counts


def _source_mask_cache_exclusion_tile_batch(
    db,
    *,
    run_id: int,
    region_id: int,
    grid_version: str,
    projected_srid: int,
    source_region_code: str,
    exclusion_layers: list[str],
    tile_limit: int,
    tile_offset: int,
) -> dict[str, Any]:
    """Measure exclusions independently of final mask components, bounded by tile.

    Union cache fragments before erosion, with a halo so cache and tile seams do
    not create artificial tolerated edges. Use all intersecting cell fragments;
    a representative point must not hide a cell crossing a tile boundary.
    """
    row = db.execute(
        text(
            """
            WITH selected_tiles AS (
                SELECT tile.id, ST_MakeEnvelope(
                    tile.min_x, tile.min_y, tile.max_x, tile.max_y, :projected_srid
                ) AS geom
                FROM stage3_grid_generation_tiles tile
                WHERE tile.run_id = :run_id
                ORDER BY tile.id
                LIMIT :tile_limit OFFSET :tile_offset
            ),
            tile_geoms AS (
                SELECT selected_tiles.*,
                    ST_Transform(geom, 4326) AS geom_4326,
                    ST_Expand(geom, :mask_edge_tolerance_m) AS halo_geom
                FROM selected_tiles
            ),
            exclusion_tile_mask AS (
                SELECT tile.id,
                    ST_MakeValid(ST_UnaryUnion(ST_Collect(
                        ST_Intersection(ST_MakeValid(cache.geom_projected), tile.halo_geom)
                    ))) AS geom
                FROM tile_geoms tile
                JOIN stage3_source_mask_cache cache
                  ON cache.source_region_code = :source_region_code
                 AND cache.projected_srid = :projected_srid
                 AND cache.source_layer = ANY(CAST(:exclusion_layers AS text[]))
                 AND cache.geom_projected && tile.halo_geom
                 AND ST_Intersects(ST_MakeValid(cache.geom_projected), tile.halo_geom)
                GROUP BY tile.id
            ),
            exclusion_interior AS (
                SELECT id, geom,
                    ST_Buffer(geom, -:mask_edge_tolerance_m) AS interior_geom
                FROM exclusion_tile_mask
            ),
            tile_cells AS (
                SELECT tile.id,
                    ST_MakeValid(ST_UnaryUnion(ST_Collect(
                        ST_Intersection(
                            ST_MakeValid(ST_Transform(ST_MakeValid(cell.geom), :projected_srid)),
                            tile.geom
                        )
                    ))) AS geom
                FROM tile_geoms tile
                JOIN grid_cells cell
                  ON cell.region_id = :region_id
                 AND cell.grid_version = :grid_version
                 AND cell.geom && tile.geom_4326
                GROUP BY tile.id
            ),
            tile_overlaps AS (
                SELECT tile.id,
                    COALESCE(ST_Area(ST_Intersection(
                        tile_cells.geom, exclusion_interior.geom
                    )), 0)::float AS raw_overlap_m2,
                    COALESCE(ST_Area(ST_Intersection(
                        tile_cells.geom, exclusion_interior.interior_geom
                    )), 0)::float AS interior_overlap_m2
                FROM tile_geoms tile
                LEFT JOIN tile_cells ON tile_cells.id = tile.id
                LEFT JOIN exclusion_interior ON exclusion_interior.id = tile.id
            )
            SELECT count(*)::bigint AS measured_tiles,
                count(*) FILTER (WHERE raw_overlap_m2 > 0)::bigint
                    AS raw_tiles_intersect_nonforest_exclusion,
                count(*) FILTER (WHERE interior_overlap_m2 > 0)::bigint
                    AS interior_tiles_intersect_nonforest_exclusion,
                COALESCE(sum(raw_overlap_m2), 0)::float AS raw_overlap_m2,
                COALESCE(sum(interior_overlap_m2), 0)::float AS interior_overlap_m2
            FROM tile_overlaps
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "grid_version": grid_version,
            "projected_srid": projected_srid,
            "source_region_code": source_region_code,
            "exclusion_layers": exclusion_layers,
            "tile_limit": tile_limit,
            "tile_offset": tile_offset,
            "mask_edge_tolerance_m": MASK_EDGE_TOLERANCE_M,
        },
    ).mappings().one()
    return dict(row)


def _source_mask_cache_tile_batch(
    db,
    *,
    run_id: int,
    region_id: int,
    grid_version: str,
    projected_srid: int,
    source_region_code: str,
    source_layers: list[str],
    intersect_layers: list[str],
    exclusion_layers: list[str],
    tile_limit: int,
    tile_offset: int,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH selected_tiles AS (
                SELECT
                    tile.id,
                    tile.status,
                    ST_MakeEnvelope(
                        tile.min_x,
                        tile.min_y,
                        tile.max_x,
                        tile.max_y,
                        :projected_srid
                    ) AS geom
                FROM stage3_grid_generation_tiles tile
                WHERE tile.run_id = :run_id
                ORDER BY tile.id
                LIMIT :tile_limit
                OFFSET :tile_offset
            ),
            tile_geoms AS (
                SELECT
                    selected_tiles.*,
                    ST_Transform(selected_tiles.geom, 4326) AS geom_4326
                FROM selected_tiles
            ),
            source_tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(ST_MakeValid(cache.geom_projected), tile.geom)
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                JOIN stage3_source_mask_cache cache
                  ON cache.source_region_code = :source_region_code
                 AND cache.projected_srid = :projected_srid
                 AND cache.source_layer = ANY(CAST(:source_layers AS text[]))
                 AND cache.geom_projected && tile.geom
                 AND ST_Intersects(ST_MakeValid(cache.geom_projected), tile.geom)
                GROUP BY tile.id
            ),
            intersect_tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(ST_MakeValid(cache.geom_projected), tile.geom)
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                JOIN stage3_source_mask_cache cache
                  ON :use_intersect_mask
                 AND cache.source_region_code = :source_region_code
                 AND cache.projected_srid = :projected_srid
                 AND cache.source_layer = ANY(CAST(:intersect_layers AS text[]))
                 AND cache.geom_projected && tile.geom
                 AND ST_Intersects(ST_MakeValid(cache.geom_projected), tile.geom)
                GROUP BY tile.id
            ),
            forest_tile_mask AS (
                SELECT
                    source_tile_mask.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                CASE
                                    WHEN :use_intersect_mask
                                      AND intersect_tile_mask.geom IS NOT NULL
                                      AND NOT ST_IsEmpty(intersect_tile_mask.geom)
                                    THEN ST_Intersection(
                                        ST_MakeValid(source_tile_mask.geom),
                                        ST_MakeValid(intersect_tile_mask.geom)
                                    )
                                    WHEN :use_intersect_mask
                                    THEN NULL
                                    ELSE source_tile_mask.geom
                                END
                            ),
                            3
                        )
                    ) AS geom
                FROM source_tile_mask
                LEFT JOIN intersect_tile_mask ON intersect_tile_mask.id = source_tile_mask.id
                WHERE source_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source_tile_mask.geom)
            ),
            exclusion_tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(ST_MakeValid(cache.geom_projected), tile.geom)
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                JOIN stage3_source_mask_cache cache
                  ON :use_exclusion_mask
                 AND cache.source_region_code = :source_region_code
                 AND cache.projected_srid = :projected_srid
                 AND cache.source_layer = ANY(CAST(:exclusion_layers AS text[]))
                 AND cache.geom_projected && tile.geom
                 AND ST_Intersects(ST_MakeValid(cache.geom_projected), tile.geom)
                GROUP BY tile.id
            ),
            tile_mask AS (
                SELECT
                    forest_tile_mask.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                CASE
                                    WHEN :use_exclusion_mask
                                      AND exclusion_tile_mask.geom IS NOT NULL
                                      AND NOT ST_IsEmpty(exclusion_tile_mask.geom)
                                    THEN ST_Difference(
                                        ST_MakeValid(forest_tile_mask.geom),
                                        ST_MakeValid(exclusion_tile_mask.geom)
                                    )
                                    ELSE forest_tile_mask.geom
                                END
                            ),
                            3
                        )
                    ) AS geom
                FROM forest_tile_mask
                LEFT JOIN exclusion_tile_mask ON exclusion_tile_mask.id = forest_tile_mask.id
                WHERE forest_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(forest_tile_mask.geom)
            ),
            tile_cells AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(
                                            ST_MakeValid(
                                                ST_Transform(ST_MakeValid(cell.geom), :projected_srid)
                                            ),
                                            tile.geom
                                        )
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                JOIN grid_cells cell
                 ON cell.region_id = :region_id
                 AND cell.grid_version = :grid_version
                 AND cell.geom && tile.geom_4326
                 AND ST_Covers(tile.geom, ST_Transform(ST_PointOnSurface(cell.geom), :projected_srid))
                GROUP BY tile.id
            ),
            empty_geom AS (
                SELECT ST_GeomFromText('POLYGON EMPTY', :projected_srid) AS geom
            ),
            tile_seams AS (
                SELECT
                    tile_mask.id,
                    ST_UnaryUnion(ST_Collect(
                        ST_Buffer(ST_Boundary(tile_geoms.geom), :tile_seam_tolerance_m),
                        ST_Buffer(ST_Boundary(tile_mask.geom), :mask_edge_tolerance_m)
                    )) AS geom
                FROM tile_mask
                JOIN tile_geoms ON tile_geoms.id = tile_mask.id
            ),
            tile_differences AS (
                SELECT
                    tile_mask.id,
                    ST_Area(tile_mask.geom)::float AS source_mask_area_m2,
                    ST_Area(
                        ST_Difference(
                            ST_MakeValid(tile_mask.geom),
                            ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom))
                        )
                    )::float AS raw_uncovered_forest_area_m2,
                    ST_Area(
                        ST_Difference(
                            ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom)),
                            ST_MakeValid(tile_mask.geom)
                        )
                    )::float AS raw_outside_forest_mask_m2,
                    ST_Area(
                        ST_Difference(
                            ST_Difference(
                                ST_MakeValid(tile_mask.geom),
                                ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom))
                            ),
                            tile_seams.geom
                        )
                    )::float AS uncovered_forest_area_m2,
                    ST_Area(
                        ST_Difference(
                            ST_Difference(
                                ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom)),
                                ST_MakeValid(tile_mask.geom)
                            ),
                            tile_seams.geom
                        )
                    )::float AS outside_forest_mask_m2,
                    COALESCE(
                        ST_Area(
                            ST_Intersection(
                                ST_MakeValid(COALESCE(tile_cells.geom, empty_geom.geom)),
                                ST_MakeValid(exclusion_tile_mask.geom)
                            )
                        ),
                        0
                    )::float AS nonforest_exclusion_overlap_m2
                FROM tile_mask
                CROSS JOIN empty_geom
                JOIN tile_seams ON tile_seams.id = tile_mask.id
                LEFT JOIN tile_cells ON tile_cells.id = tile_mask.id
                LEFT JOIN exclusion_tile_mask ON exclusion_tile_mask.id = tile_mask.id
                WHERE tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(tile_mask.geom)
            )
            SELECT
                count(tile_geoms.id)::int AS tile_count,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'completed')::int AS completed_tiles,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'pending')::int AS pending_tiles,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'running')::int AS running_tiles,
                count(tile_geoms.id) FILTER (WHERE tile_geoms.status = 'failed')::int AS failed_tiles,
                count(tile_mask.id)::int AS source_mask_tiles,
                count(exclusion_tile_mask.id)::int AS exclusion_mask_tiles,
                count(tile_differences.id) FILTER (
                    WHERE tile_differences.outside_forest_mask_m2 > 0.01
                )::int AS tiles_outside_forest_mask,
                count(tile_differences.id) FILTER (
                    WHERE tile_differences.raw_outside_forest_mask_m2 > 0.01
                )::int AS raw_tiles_outside_forest_mask,
                count(tile_differences.id) FILTER (
                    WHERE tile_differences.nonforest_exclusion_overlap_m2 > 0.01
                )::int AS tiles_intersect_nonforest_exclusion,
                COALESCE(sum(tile_differences.source_mask_area_m2), 0)::float AS source_mask_area_m2,
                COALESCE(sum(tile_differences.uncovered_forest_area_m2), 0)::float
                    AS uncovered_forest_area_m2,
                COALESCE(sum(tile_differences.outside_forest_mask_m2), 0)::float
                    AS outside_forest_mask_m2,
                COALESCE(sum(tile_differences.raw_uncovered_forest_area_m2), 0)::float
                    AS raw_uncovered_forest_area_m2,
                COALESCE(sum(tile_differences.raw_outside_forest_mask_m2), 0)::float
                    AS raw_outside_forest_mask_m2,
                COALESCE(sum(tile_differences.nonforest_exclusion_overlap_m2), 0)::float
                    AS nonforest_exclusion_overlap_m2
            FROM tile_geoms
            LEFT JOIN tile_mask ON tile_mask.id = tile_geoms.id
            LEFT JOIN exclusion_tile_mask ON exclusion_tile_mask.id = tile_geoms.id
            LEFT JOIN tile_differences ON tile_differences.id = tile_geoms.id
            """
        ),
        {
            "run_id": run_id,
            "region_id": region_id,
            "grid_version": grid_version,
            "projected_srid": projected_srid,
            "source_region_code": source_region_code,
            "source_layers": source_layers,
            "intersect_layers": intersect_layers or ["__unused__"],
            "exclusion_layers": exclusion_layers or ["__unused__"],
            "use_intersect_mask": bool(intersect_layers),
            "use_exclusion_mask": bool(exclusion_layers),
            "tile_limit": tile_limit,
            "tile_offset": tile_offset,
            "tile_seam_tolerance_m": TILE_SEAM_TOLERANCE_M,
            "mask_edge_tolerance_m": MASK_EDGE_TOLERANCE_M,
        },
    ).mappings().one()
    return dict(row)


def _source_mask_scale_tolerance_m2(source_area_m2: float) -> float:
    scaled = max(0.0, float(source_area_m2 or 0)) * SOURCE_MASK_SCALE_TOLERANCE_RATIO
    return max(FOREST_COVERAGE_TOLERANCE_M2, min(SOURCE_MASK_SCALE_TOLERANCE_CAP_M2, scaled))


def _source_mask_forest_clip_report(
    db,
    *,
    context: dict[str, Any],
    region_code: str,
    grid_version: str,
    expected_area_ha: float = 1.0,
) -> dict:
    row = db.execute(
        text(
            """
            WITH region AS (
                SELECT id, metadata
                FROM regions
                WHERE code = :region_code
            ),
            grid_run AS (
                SELECT run.id,
                       run.region_id,
                       run.grid_size_m,
                       run.projected_srid,
                       run.status,
                       run.total_tiles,
                       run.processed_tiles,
                       run.metadata
                FROM stage3_grid_generation_runs run
                JOIN region ON region.id = run.region_id
                WHERE run.grid_version = :grid_version
                ORDER BY run.id DESC
                LIMIT 1
            ),
            source_layers AS (
                SELECT jsonb_array_elements_text(COALESCE(grid_run.metadata->'mask_source_layers', '[]'::jsonb)) AS source_layer
                FROM grid_run
            ),
            intersect_layers AS (
                SELECT jsonb_array_elements_text(
                    COALESCE(grid_run.metadata->'mask_intersect_source_layers', '[]'::jsonb)
                ) AS source_layer
                FROM grid_run
            ),
            exclusion_layers AS (
                SELECT jsonb_array_elements_text(
                    COALESCE(grid_run.metadata->'exclude_source_layers', '[]'::jsonb)
                ) AS source_layer
                FROM grid_run
            ),
            flags AS (
                SELECT
                    EXISTS (SELECT 1 FROM intersect_layers) AS use_intersect_mask,
                    EXISTS (SELECT 1 FROM exclusion_layers) AS use_exclusion_mask
            ),
            tiles AS (
                SELECT
                    tile.id,
                    tile.status,
                    tile.inserted_cells,
                    ST_MakeEnvelope(
                        tile.min_x,
                        tile.min_y,
                        tile.max_x,
                        tile.max_y,
                        grid_run.projected_srid
                    ) AS geom
                FROM stage3_grid_generation_tiles tile
                JOIN grid_run ON grid_run.id = tile.run_id
            ),
            tile_geoms AS (
                SELECT
                    tiles.*,
                    ST_Transform(tiles.geom, 4326) AS geom_4326
                FROM tiles
            ),
            source_tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(
                                            ST_Transform(ST_MakeValid(parcel.geom), grid_run.projected_srid),
                                            tile.geom
                                        )
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                CROSS JOIN grid_run
                JOIN ogm_parcels parcel
                  ON parcel.is_current
                 AND parcel.source_layer IN (SELECT source_layer FROM source_layers)
                 AND parcel.attributes->>'region_code' = COALESCE(
                        grid_run.metadata->>'mask_source_region_code',
                        :region_code
                     )
                 AND parcel.geom && tile.geom_4326
                 AND ST_Intersects(parcel.geom, tile.geom_4326)
                GROUP BY tile.id
            ),
            intersect_tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(
                                            ST_Transform(ST_MakeValid(parcel.geom), grid_run.projected_srid),
                                            tile.geom
                                        )
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                CROSS JOIN grid_run
                CROSS JOIN flags
                JOIN ogm_parcels parcel
                  ON flags.use_intersect_mask
                 AND parcel.is_current
                 AND parcel.source_layer IN (SELECT source_layer FROM intersect_layers)
                 AND parcel.attributes->>'region_code' = COALESCE(
                        grid_run.metadata->>'mask_source_region_code',
                        :region_code
                     )
                 AND parcel.geom && tile.geom_4326
                 AND ST_Intersects(parcel.geom, tile.geom_4326)
                GROUP BY tile.id
            ),
            forest_tile_mask AS (
                SELECT
                    source_tile_mask.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                CASE
                                    WHEN flags.use_intersect_mask
                                      AND intersect_tile_mask.geom IS NOT NULL
                                      AND NOT ST_IsEmpty(intersect_tile_mask.geom)
                                    THEN ST_Intersection(source_tile_mask.geom, intersect_tile_mask.geom)
                                    WHEN flags.use_intersect_mask
                                    THEN NULL
                                    ELSE source_tile_mask.geom
                                END
                            ),
                            3
                        )
                    ) AS geom
                FROM source_tile_mask
                CROSS JOIN flags
                LEFT JOIN intersect_tile_mask ON intersect_tile_mask.id = source_tile_mask.id
                WHERE source_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source_tile_mask.geom)
            ),
            exclusion_tile_mask AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(
                                            ST_Transform(ST_MakeValid(parcel.geom), grid_run.projected_srid),
                                            tile.geom
                                        )
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                CROSS JOIN grid_run
                CROSS JOIN flags
                JOIN ogm_parcels parcel
                  ON flags.use_exclusion_mask
                 AND parcel.is_current
                 AND parcel.source_layer IN (SELECT source_layer FROM exclusion_layers)
                 AND parcel.attributes->>'region_code' = COALESCE(
                        grid_run.metadata->>'mask_source_region_code',
                        :region_code
                     )
                 AND parcel.geom && tile.geom_4326
                 AND ST_Intersects(parcel.geom, tile.geom_4326)
                GROUP BY tile.id
            ),
            tile_mask AS (
                SELECT
                    forest_tile_mask.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                CASE
                                    WHEN flags.use_exclusion_mask
                                      AND exclusion_tile_mask.geom IS NOT NULL
                                      AND NOT ST_IsEmpty(exclusion_tile_mask.geom)
                                    THEN ST_Difference(forest_tile_mask.geom, exclusion_tile_mask.geom)
                                    ELSE forest_tile_mask.geom
                                END
                            ),
                            3
                        )
                    ) AS geom
                FROM forest_tile_mask
                CROSS JOIN flags
                LEFT JOIN exclusion_tile_mask ON exclusion_tile_mask.id = forest_tile_mask.id
                WHERE forest_tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(forest_tile_mask.geom)
            ),
            cells AS (
                SELECT cell.id, cell.geom, cell.area_ha
                FROM grid_cells cell
                JOIN region ON region.id = cell.region_id
                WHERE cell.grid_version = :grid_version
            ),
            tile_cells AS (
                SELECT
                    tile.id,
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_UnaryUnion(
                                    ST_Collect(
                                        ST_Intersection(
                                            ST_Transform(ST_MakeValid(cell.geom), grid_run.projected_srid),
                                            tile.geom
                                        )
                                    )
                                )
                            ),
                            3
                        )
                    ) AS geom
                FROM tile_geoms tile
                CROSS JOIN grid_run
                JOIN cells cell
                  ON cell.geom && tile.geom_4326
                 AND ST_Intersects(cell.geom, tile.geom_4326)
                GROUP BY tile.id
            ),
            empty_geom AS (
                SELECT ST_GeomFromText('POLYGON EMPTY', grid_run.projected_srid) AS geom
                FROM grid_run
            ),
            tile_differences AS (
                SELECT
                    tile_mask.id,
                    ST_Area(tile_mask.geom)::float AS source_mask_area_m2,
                    ST_Area(
                        ST_Difference(tile_mask.geom, COALESCE(tile_cells.geom, empty_geom.geom))
                    )::float AS uncovered_forest_area_m2,
                    ST_Area(
                        ST_Difference(COALESCE(tile_cells.geom, empty_geom.geom), tile_mask.geom)
                    )::float AS outside_forest_mask_m2,
                    COALESCE(
                        ST_Area(
                            ST_Intersection(COALESCE(tile_cells.geom, empty_geom.geom), exclusion_tile_mask.geom)
                        ),
                        0
                    )::float AS nonforest_exclusion_overlap_m2
                FROM tile_mask
                CROSS JOIN empty_geom
                LEFT JOIN tile_cells ON tile_cells.id = tile_mask.id
                LEFT JOIN exclusion_tile_mask ON exclusion_tile_mask.id = tile_mask.id
                WHERE tile_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(tile_mask.geom)
            ),
            cell_area AS (
                SELECT COALESCE(sum(ST_Area(ST_Transform(cells.geom, grid_run.projected_srid))), 0)::float AS active_grid_area_m2
                FROM cells
                CROSS JOIN grid_run
            )
            SELECT
                (SELECT count(*) FROM cells)::int AS cell_count,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE geom IS NULL
                       OR ST_IsEmpty(geom)
                       OR NOT ST_IsValid(geom)
                )::int AS invalid_cells,
                (
                    SELECT count(*)
                    FROM tile_differences
                    WHERE outside_forest_mask_m2 > 0.01
                )::int AS cells_outside_forest_mask,
                (
                    SELECT COALESCE(sum(outside_forest_mask_m2), 0)
                    FROM tile_differences
                )::float AS total_outside_forest_mask_m2,
                (
                    SELECT count(*)
                    FROM tile_differences
                    WHERE nonforest_exclusion_overlap_m2 > 0.01
                )::int AS cells_intersect_nonforest_exclusion,
                (
                    SELECT COALESCE(sum(nonforest_exclusion_overlap_m2), 0)
                    FROM tile_differences
                )::float AS total_nonforest_exclusion_overlap_m2,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE ST_Area(geom::geography) / 10000.0 > (:expected_area_ha * 1.01)
                )::int AS oversized_cells,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE ST_Area(geom::geography) / 10000.0 < (:expected_area_ha * 0.99)
                )::int AS clipped_edge_cells,
                (
                    SELECT count(*)
                    FROM cells
                    WHERE GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
                )::int AS polygonal_cells,
                COALESCE(
                    (
                        SELECT (
                            sum(uncovered_forest_area_m2) + sum(outside_forest_mask_m2)
                        ) / NULLIF(sum(source_mask_area_m2), 0)
                        FROM tile_differences
                    ),
                    1
                )::float AS rebuilt_mask_difference_ratio,
                (
                    SELECT count(*) > 0
                    FROM tile_mask
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                ) AS rebuilt_mask_available,
                (
                    SELECT count(*) > 0
                    FROM exclusion_tile_mask
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                ) AS rebuilt_difference_mask_available,
                COALESCE(
                    1 - (
                        (SELECT sum(uncovered_forest_area_m2) FROM tile_differences)
                        / NULLIF((SELECT sum(source_mask_area_m2) FROM tile_differences), 0)
                    ),
                    0
                )::float AS forest_mask_coverage_ratio,
                COALESCE((SELECT sum(source_mask_area_m2) FROM tile_differences) / 10000.0, 0)::float AS forest_mask_area_ha,
                COALESCE(cell_area.active_grid_area_m2 / 10000.0, 0)::float AS active_grid_area_ha,
                COALESCE((SELECT sum(uncovered_forest_area_m2) FROM tile_differences), 0)::float AS uncovered_forest_area_m2,
                COALESCE((SELECT sum(uncovered_forest_area_m2) FROM tile_differences) / 10000.0, 0)::float AS uncovered_forest_area_ha,
                (SELECT count(*) FROM tile_geoms WHERE status = 'completed')::int AS completed_tiles,
                (SELECT count(*) FROM tile_geoms WHERE status = 'pending')::int AS pending_tiles,
                (SELECT count(*) FROM tile_geoms WHERE status = 'running')::int AS running_tiles,
                (SELECT count(*) FROM tile_geoms WHERE status = 'failed')::int AS failed_tiles,
                (SELECT status FROM grid_run) AS grid_run_status,
                (SELECT metadata FROM region) AS region_metadata
            FROM grid_run
            CROSS JOIN cell_area
            """
        ),
        {"region_code": region_code, "grid_version": grid_version, "expected_area_ha": expected_area_ha},
    ).mappings().one()
    metadata = _json_dict(row["region_metadata"] or context.get("region_metadata") or {})
    total_outside_m2 = _float_value(row, "total_outside_forest_mask_m2")
    uncovered_m2 = _float_value(row, "uncovered_forest_area_m2")
    rebuilt_mask_difference_ratio = (
        1.0 if row["rebuilt_mask_difference_ratio"] is None else float(row["rebuilt_mask_difference_ratio"])
    )
    all_tiles_completed = (
        str(row["grid_run_status"] or context.get("grid_run_status") or "") == "completed"
        and int(row["pending_tiles"] or 0) == 0
        and int(row["running_tiles"] or 0) == 0
        and int(row["failed_tiles"] or 0) == 0
    )
    checks = {
        "has_cells": int(row["cell_count"] or 0) > 0,
        "valid_geometry": int(row["invalid_cells"] or 0) == 0,
        "all_cells_inside_forest_mask": total_outside_m2 <= FOREST_COVERAGE_TOLERANCE_M2,
        "no_cells_intersect_nonforest_exclusion": _float_value(row, "total_nonforest_exclusion_overlap_m2")
        <= FOREST_COVERAGE_TOLERANCE_M2,
        "no_oversized_cells": int(row["oversized_cells"] or 0) == 0,
        "polygonal_cell_geometry": int(row["polygonal_cells"] or 0) == int(row["cell_count"] or 0),
        "forest_mask_covered_by_clipped_grid": uncovered_m2 <= FOREST_COVERAGE_TOLERANCE_M2,
        "forest_mask_has_public_osm_clip": public_forest_mask_metadata_ready(metadata),
        "forest_mask_has_public_nonforest_difference": _public_nonforest_difference_metadata_ready(metadata),
        "forest_mask_has_clean_component_policy": _clean_mask_policy_ready(metadata),
        "rebuilt_ogm_osm_mask_available": bool(row["rebuilt_mask_available"]),
        "rebuilt_nonforest_difference_available": bool(row["rebuilt_difference_mask_available"]),
        "region_matches_rebuilt_ogm_osm_mask": rebuilt_mask_difference_ratio <= 0.001,
        "all_source_mask_tiles_completed": all_tiles_completed,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "cell_count": int(row["cell_count"] or 0),
        "invalid_cells": int(row["invalid_cells"] or 0),
        "cells_outside_forest_mask": int(row["cells_outside_forest_mask"] or 0),
        "total_outside_forest_mask_m2": round(total_outside_m2, 6),
        "cells_intersect_nonforest_exclusion": int(row["cells_intersect_nonforest_exclusion"] or 0),
        "total_nonforest_exclusion_overlap_m2": round(
            _float_value(row, "total_nonforest_exclusion_overlap_m2"),
            6,
        ),
        "oversized_cells": int(row["oversized_cells"] or 0),
        "clipped_edge_cells": int(row["clipped_edge_cells"] or 0),
        "polygonal_cells": int(row["polygonal_cells"] or 0),
        "rebuilt_mask_difference_ratio": round(rebuilt_mask_difference_ratio, 6),
        "forest_mask_coverage_ratio": round(float(row["forest_mask_coverage_ratio"] or 0), 5),
        "forest_mask_area_ha": round(_float_value(row, "forest_mask_area_ha"), 4),
        "active_grid_area_ha": round(_float_value(row, "active_grid_area_ha"), 4),
        "uncovered_forest_area_m2": round(uncovered_m2, 6),
        "uncovered_forest_area_ha": round(_float_value(row, "uncovered_forest_area_ha"), 4),
        "completed_tiles": int(row["completed_tiles"] or 0),
        "pending_tiles": int(row["pending_tiles"] or 0),
        "running_tiles": int(row["running_tiles"] or 0),
        "failed_tiles": int(row["failed_tiles"] or 0),
        "forest_coverage_tolerance_m2": FOREST_COVERAGE_TOLERANCE_M2,
        "coverage_note": (
            "Stage 3 stores the clipped forest geometry for each 100 m grid index. "
            "Full public source-mask runs are checked per tile to avoid a national-scale union geometry."
        ),
        "forest_mask_source": (metadata.get("forest_mask") or {}).get("source"),
        "forest_mask_method": (metadata.get("forest_mask") or {}).get("method"),
        "clean_mask_policy": (metadata.get("forest_mask") or {}).get("clean_mask_policy") or {},
        "checks": checks,
    }


def _public_nonforest_difference_metadata_ready(metadata: dict[str, Any]) -> bool:
    forest_mask = metadata.get("forest_mask") or {}
    if forest_mask.get("source") not in {
        "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference",
        "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
        "public_osm_forest_mask_with_nonforest_difference",
    }:
        return False
    difference_layers = set(forest_mask.get("difference_source_layers") or [])
    difference_qualities = set(forest_mask.get("difference_source_qualities") or [])
    difference_roles = set(forest_mask.get("difference_production_roles") or [])
    allowed_layers = {
        SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
        SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION,
    }
    allowed_qualities = {
        "public_osm_nonforest_exclusion",
        "public_satellite_landcover_nonforest_exclusion",
    }
    return (
        SOURCE_LAYER_OSM_NONFOREST_EXCLUSION in difference_layers
        and difference_layers <= allowed_layers
        and "public_osm_nonforest_exclusion" in difference_qualities
        and difference_qualities <= allowed_qualities
        and difference_roles == {"auxiliary_public_nonforest_exclusion"}
    )


def _clean_mask_policy_ready(metadata: dict[str, Any]) -> bool:
    forest_mask = metadata.get("forest_mask") or {}
    policy = forest_mask.get("clean_mask_policy") or {}
    if not isinstance(policy, dict):
        return False
    try:
        min_component_area_ha = float(policy.get("min_component_area_ha") or 0)
    except (TypeError, ValueError):
        return False
    return (
        policy.get("applies_before_grid_insert") is True
        and policy.get("method")
        in {
            "component_area_filter_before_grid_generation",
            "component_area_filter_before_grid_insert",
            "global_connected_component_area_filter_before_grid_generation",
        }
        and min_component_area_ha > 0
    )


def _float_value(row: dict, key: str) -> float:
    value = row.get(key) if hasattr(row, "get") else row[key] if key in row else 0
    return float(value or 0)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value or "{}")
    return dict(value or {})


if __name__ == "__main__":
    main()
