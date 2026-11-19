from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal


DEFAULT_PROJECTED_SRID = 6933


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check that a Stage 3 grid was generated from the prepared clean mask components"
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", required=True)
    parser.add_argument("--mask-version", default="")
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--min-component-area-ha", type=float, default=1.0)
    parser.add_argument("--max-area-gap-ha", type=float, default=2.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.min_component_area_ha < 0:
        raise ValueError("min-component-area-ha must not be negative")
    if args.max_area_gap_ha < 0:
        raise ValueError("max-area-gap-ha must not be negative")

    with SessionLocal() as db:
        report = grid_mask_proof_report(
            db,
            region_code=args.region_code,
            grid_version=args.grid_version,
            mask_version=args.mask_version or args.region_code,
            projected_srid=args.projected_srid,
            min_component_area_ha=args.min_component_area_ha,
            max_area_gap_ha=args.max_area_gap_ha,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


def grid_mask_proof_report(
    db,
    *,
    region_code: str,
    grid_version: str,
    mask_version: str,
    projected_srid: int = DEFAULT_PROJECTED_SRID,
    min_component_area_ha: float = 1.0,
    max_area_gap_ha: float = 2.0,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH region_scope AS (
                SELECT id, metadata
                FROM regions
                WHERE code = :region_code
            ),
            latest_run AS (
                SELECT run.*
                FROM stage3_grid_generation_runs run
                JOIN region_scope region ON region.id = run.region_id
                WHERE run.grid_version = :grid_version
                ORDER BY run.id DESC
                LIMIT 1
            ),
            tile_metrics AS (
                SELECT
                    count(tile.id)::bigint AS tile_count,
                    count(tile.id) FILTER (WHERE tile.status = 'completed')::bigint AS completed_tiles,
                    count(tile.id) FILTER (WHERE tile.status = 'failed')::bigint AS failed_tiles,
                    count(tile.id) FILTER (WHERE tile.status IN ('pending', 'running'))::bigint AS unfinished_tiles
                FROM latest_run run
                LEFT JOIN stage3_grid_generation_tiles tile ON tile.run_id = run.id
            ),
            component_metrics AS (
                SELECT
                    count(component.id)::bigint AS component_count,
                    count(component.id) FILTER (
                        WHERE component.geom_projected IS NULL
                           OR ST_IsEmpty(component.geom_projected)
                    )::bigint AS empty_components,
                    count(component.id) FILTER (
                        WHERE NOT ST_IsValid(component.geom_projected)
                    )::bigint AS invalid_components,
                    count(component.id) FILTER (
                        WHERE (
                            CASE
                                WHEN component.metadata ? 'connected_component_area_ha'
                                THEN NULLIF(component.metadata->>'connected_component_area_ha', '')::float
                                ELSE ST_Area(component.geom_projected) / 10000.0
                            END
                        ) < :min_component_area_ha
                    )::bigint AS components_below_min_area,
                    COALESCE(sum(ST_Area(component.geom_projected) / 10000.0), 0)::float AS component_area_ha
                FROM region_scope region
                LEFT JOIN stage3_region_mask_components component
                  ON component.region_id = region.id
                 AND component.mask_version = :mask_version
                 AND component.projected_srid = :projected_srid
            ),
            grid_metrics AS (
                SELECT
                    count(cell.id)::bigint AS cell_count,
                    COALESCE(sum(cell.area_ha), 0)::float AS cell_area_ha,
                    COALESCE(min(cell.area_ha), 0)::float AS min_cell_area_ha,
                    COALESCE(max(cell.area_ha), 0)::float AS max_cell_area_ha
                FROM region_scope region
                LEFT JOIN grid_cells cell
                  ON cell.region_id = region.id
                 AND cell.grid_version = :grid_version
            )
            SELECT
                region_scope.metadata AS region_metadata,
                latest_run.status AS run_status,
                latest_run.total_tiles,
                latest_run.processed_tiles,
                latest_run.inserted_cells,
                latest_run.metadata AS run_metadata,
                tile_metrics.*,
                component_metrics.*,
                grid_metrics.*
            FROM region_scope
            LEFT JOIN latest_run ON true
            CROSS JOIN tile_metrics
            CROSS JOIN component_metrics
            CROSS JOIN grid_metrics
            """
        ),
        {
            "region_code": region_code,
            "grid_version": grid_version,
            "mask_version": mask_version,
            "projected_srid": projected_srid,
            "min_component_area_ha": min_component_area_ha,
        },
    ).mappings().one()

    run_metadata = _json_dict(row["run_metadata"])
    region_metadata = _json_dict(row["region_metadata"])
    clean_policy = run_metadata.get("clean_mask_policy") or (
        (region_metadata.get("forest_mask") or {}).get("clean_mask_policy") or {}
    )
    component_area_ha = float(row["component_area_ha"] or 0)
    cell_area_ha = float(row["cell_area_ha"] or 0)
    area_gap_ha = abs(component_area_ha - cell_area_ha)
    tolerance_ha = max(float(max_area_gap_ha), int(row["cell_count"] or 0) * 0.00005)
    checks = {
        "run_completed": row["run_status"] == "completed",
        "all_tiles_completed": int(row["total_tiles"] or 0) > 0
        and int(row["processed_tiles"] or 0) == int(row["total_tiles"] or 0)
        and int(row["completed_tiles"] or 0) == int(row["total_tiles"] or 0)
        and int(row["failed_tiles"] or 0) == 0
        and int(row["unfinished_tiles"] or 0) == 0,
        "generated_from_region_mask_components": run_metadata.get("grid_mask_method") == "region_mask_components",
        "hard_difference_is_osm_nonforest": (
            ((run_metadata.get("forest_mask") or {}).get("difference_source_layers") or [])
            == ["osm_nonforest_exclusion"]
        ),
        "clean_policy_before_insert": bool(clean_policy.get("applies_before_grid_insert")),
        "component_threshold_recorded": float(clean_policy.get("min_component_area_ha") or 0)
        >= float(min_component_area_ha),
        "components_present": int(row["component_count"] or 0) > 0,
        "components_valid": int(row["invalid_components"] or 0) == 0,
        "components_nonempty": int(row["empty_components"] or 0) == 0,
        "no_tiny_components": int(row["components_below_min_area"] or 0) == 0,
        "grid_present": int(row["cell_count"] or 0) > 0,
        "run_insert_count_matches_grid": int(row["inserted_cells"] or 0) == int(row["cell_count"] or 0),
        "grid_area_matches_mask_area": area_gap_ha <= tolerance_ha,
        "cell_area_cap_ok": float(row["max_cell_area_ha"] or 0) <= 1.0002,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "mask_version": mask_version,
        "projected_srid": projected_srid,
        "run_status": row["run_status"],
        "total_tiles": int(row["total_tiles"] or 0),
        "processed_tiles": int(row["processed_tiles"] or 0),
        "completed_tiles": int(row["completed_tiles"] or 0),
        "failed_tiles": int(row["failed_tiles"] or 0),
        "unfinished_tiles": int(row["unfinished_tiles"] or 0),
        "inserted_cells": int(row["inserted_cells"] or 0),
        "cell_count": int(row["cell_count"] or 0),
        "component_count": int(row["component_count"] or 0),
        "component_area_ha": round(component_area_ha, 6),
        "cell_area_ha": round(cell_area_ha, 6),
        "area_gap_ha": round(area_gap_ha, 6),
        "area_tolerance_ha": round(tolerance_ha, 6),
        "min_cell_area_ha": round(float(row["min_cell_area_ha"] or 0), 6),
        "max_cell_area_ha": round(float(row["max_cell_area_ha"] or 0), 6),
        "clean_mask_policy": clean_policy,
        "grid_mask_method": run_metadata.get("grid_mask_method"),
        "difference_source_layers": (run_metadata.get("forest_mask") or {}).get("difference_source_layers") or [],
        "checks": checks,
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return {}


if __name__ == "__main__":
    main()
