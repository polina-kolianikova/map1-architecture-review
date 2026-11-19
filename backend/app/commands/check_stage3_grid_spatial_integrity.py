from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import NoResultFound

from app.db.session import SessionLocal


DEFAULT_PROJECTED_SRID = 6933


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strict tile-by-tile spatial integrity proof for a Stage 3 grid."
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", required=True)
    parser.add_argument("--mask-version", default="")
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--max-tile-gap-m2", type=float, default=200.0)
    parser.add_argument("--max-total-gap-m2", type=float, default=1000.0)
    parser.add_argument("--tile-limit", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.max_tile_gap_m2 < 0 or args.max_total_gap_m2 < 0:
        raise ValueError("gap tolerances must not be negative")
    if args.tile_limit < 0:
        raise ValueError("tile-limit must not be negative")

    with SessionLocal() as db:
        report = grid_spatial_integrity_report(
            db,
            region_code=args.region_code,
            grid_version=args.grid_version,
            mask_version=args.mask_version or args.region_code,
            projected_srid=args.projected_srid,
            max_tile_gap_m2=args.max_tile_gap_m2,
            max_total_gap_m2=args.max_total_gap_m2,
            tile_limit=args.tile_limit,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


def grid_spatial_integrity_report(
    db,
    *,
    region_code: str,
    grid_version: str,
    mask_version: str,
    projected_srid: int,
    max_tile_gap_m2: float = 200.0,
    max_total_gap_m2: float = 1000.0,
    tile_limit: int = 0,
) -> dict[str, Any]:
    try:
        run = db.execute(
            text(
                """
                WITH region_scope AS (
                    SELECT id
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
                )
                SELECT
                    latest_run.id AS run_id,
                    latest_run.region_id,
                    latest_run.status,
                    latest_run.total_tiles,
                    latest_run.processed_tiles,
                    latest_run.inserted_cells,
                    latest_run.grid_size_m,
                    latest_run.tile_size_m,
                    latest_run.metadata,
                    count(tile.id)::bigint AS tile_count,
                    count(tile.id) FILTER (WHERE tile.status = 'completed')::bigint AS completed_tiles,
                    count(tile.id) FILTER (WHERE tile.status <> 'completed')::bigint AS incomplete_tiles
                FROM latest_run
                LEFT JOIN stage3_grid_generation_tiles tile ON tile.run_id = latest_run.id
                GROUP BY
                    latest_run.id,
                    latest_run.region_id,
                    latest_run.status,
                    latest_run.total_tiles,
                    latest_run.processed_tiles,
                    latest_run.inserted_cells,
                    latest_run.grid_size_m,
                    latest_run.tile_size_m,
                    latest_run.metadata
                """
            ),
            {"region_code": region_code, "grid_version": grid_version},
        ).mappings().one()
    except NoResultFound:
        return _empty_report(region_code, grid_version, mask_version, projected_srid, "missing_grid_run")
    if run["run_id"] is None:
        return _empty_report(region_code, grid_version, mask_version, projected_srid, "missing_grid_run")
    run_id = run["run_id"]

    tiles = db.execute(
        text(
            """
            SELECT id, tile_key, min_x, min_y, max_x, max_y
            FROM stage3_grid_generation_tiles
            WHERE run_id = :run_id
              AND status = 'completed'
            ORDER BY id
            LIMIT NULLIF(:tile_limit, 0)
            """
        ),
        {"run_id": int(run_id), "tile_limit": int(tile_limit)},
    ).mappings().all()
    cell_quality = _grid_cell_quality(
        db,
        region_id=int(run["region_id"]),
        grid_version=grid_version,
    )

    totals = {
        "mask_without_cells_m2": 0.0,
        "cells_outside_mask_m2": 0.0,
        "cell_overlap_m2": 0.0,
        "invalid_cells": int(cell_quality["invalid_cells"] or 0),
        "empty_cells": int(cell_quality["empty_cells"] or 0),
        "checked_cells": 0,
        "tiles_with_gap": 0,
        "tiles_with_outside": 0,
        "tiles_with_overlap": 0,
        "tiles_checked": 0,
    }
    worst_tiles: list[dict[str, Any]] = []
    for tile in tiles:
        metrics = _tile_metrics(
            db,
            region_id=int(run["region_id"]),
            grid_version=grid_version,
            mask_version=mask_version,
            projected_srid=projected_srid,
            tile=tile,
        )
        totals["tiles_checked"] += 1
        totals["checked_cells"] += int(metrics["cell_count"] or 0)
        for key in ("mask_without_cells_m2", "cells_outside_mask_m2", "cell_overlap_m2"):
            totals[key] += float(metrics[key] or 0)
        if float(metrics["mask_without_cells_m2"] or 0) > max_tile_gap_m2:
            totals["tiles_with_gap"] += 1
        if float(metrics["cells_outside_mask_m2"] or 0) > max_tile_gap_m2:
            totals["tiles_with_outside"] += 1
        if float(metrics["cell_overlap_m2"] or 0) > max_tile_gap_m2:
            totals["tiles_with_overlap"] += 1
        tile_problem_m2 = max(
            float(metrics["mask_without_cells_m2"] or 0),
            float(metrics["cells_outside_mask_m2"] or 0),
            float(metrics["cell_overlap_m2"] or 0),
        )
        if tile_problem_m2 > max_tile_gap_m2:
            worst_tiles.append(
                {
                    "tile_id": int(tile["id"]),
                    "tile_key": str(tile["tile_key"]),
                    "mask_without_cells_m2": round(float(metrics["mask_without_cells_m2"] or 0), 6),
                    "cells_outside_mask_m2": round(float(metrics["cells_outside_mask_m2"] or 0), 6),
                    "cell_overlap_m2": round(float(metrics["cell_overlap_m2"] or 0), 6),
                    "cell_count": int(metrics["cell_count"] or 0),
                }
            )
    worst_tiles.sort(
        key=lambda item: max(
            item["mask_without_cells_m2"],
            item["cells_outside_mask_m2"],
            item["cell_overlap_m2"],
        ),
        reverse=True,
    )
    checks = {
        "run_completed": run["status"] == "completed",
        "tile_queue_count_matches_run_total": int(run["tile_count"] or 0) == int(run["total_tiles"] or 0)
        and int(run["total_tiles"] or 0) > 0,
        "processed_tiles_match_completed_tiles": int(run["processed_tiles"] or 0) == int(run["completed_tiles"] or 0)
        and int(run["processed_tiles"] or 0) == int(run["total_tiles"] or 0),
        "all_tiles_completed": int(run["tile_count"] or 0) > 0
        and int(run["incomplete_tiles"] or 0) == 0
        and int(run["completed_tiles"] or 0) == int(run["tile_count"] or 0),
        "grid_size_is_100m": int(run["grid_size_m"] or 0) == 100,
        "tile_size_aligned_to_grid": int(run["tile_size_m"] or 0) > 0
        and int(run["grid_size_m"] or 0) > 0
        and int(run["tile_size_m"] or 0) % int(run["grid_size_m"] or 1) == 0,
        "all_completed_tiles_checked": int(totals["tiles_checked"]) == int(run["completed_tiles"] or 0)
        if tile_limit == 0
        else False,
        "full_tile_scope": tile_limit == 0,
        "cells_valid": int(totals["invalid_cells"]) == 0,
        "cells_nonempty": int(totals["empty_cells"]) == 0,
        "no_mask_gap": totals["mask_without_cells_m2"] <= max_total_gap_m2 and totals["tiles_with_gap"] == 0,
        "no_cells_outside_mask": totals["cells_outside_mask_m2"] <= max_total_gap_m2
        and totals["tiles_with_outside"] == 0,
        "no_positive_cell_overlap": totals["cell_overlap_m2"] <= max_total_gap_m2
        and totals["tiles_with_overlap"] == 0,
        "run_insert_count_leq_checked_tile_parts": int(run["inserted_cells"] or 0) <= int(totals["checked_cells"])
        if tile_limit == 0
        else False,
        "persisted_cells_match_inserted_cells": int(cell_quality["persisted_cells"] or 0)
        == int(run["inserted_cells"] or 0),
        "persisted_cells_leq_checked_tile_parts": int(cell_quality["persisted_cells"] or 0)
        <= int(totals["checked_cells"])
        if tile_limit == 0
        else False,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "mask_version": mask_version,
        "projected_srid": projected_srid,
        "run_id": int(run_id),
        "run_status": run["status"],
        "total_tiles": int(run["total_tiles"] or 0),
        "processed_tiles": int(run["processed_tiles"] or 0),
        "tile_count": int(run["tile_count"] or 0),
        "completed_tiles": int(run["completed_tiles"] or 0),
        "incomplete_tiles": int(run["incomplete_tiles"] or 0),
        "grid_size_m": int(run["grid_size_m"] or 0),
        "tile_size_m": int(run["tile_size_m"] or 0),
        "tiles_checked": int(totals["tiles_checked"]),
        "checked_cells": int(totals["checked_cells"]),
        "persisted_cells": int(cell_quality["persisted_cells"] or 0),
        "inserted_cells": int(run["inserted_cells"] or 0),
        "invalid_cells": int(totals["invalid_cells"]),
        "empty_cells": int(totals["empty_cells"]),
        "mask_without_cells_m2": round(float(totals["mask_without_cells_m2"]), 6),
        "cells_outside_mask_m2": round(float(totals["cells_outside_mask_m2"]), 6),
        "cell_overlap_m2": round(float(totals["cell_overlap_m2"]), 6),
        "tiles_with_gap": int(totals["tiles_with_gap"]),
        "tiles_with_outside": int(totals["tiles_with_outside"]),
        "tiles_with_overlap": int(totals["tiles_with_overlap"]),
        "worst_tiles": worst_tiles[:10],
        "checks": checks,
    }


def _grid_cell_quality(db, *, region_id: int, grid_version: str):
    return db.execute(
        text(
            """
            SELECT
                count(*)::bigint AS persisted_cells,
                count(*) FILTER (
                    WHERE geom IS NULL OR ST_IsEmpty(geom)
                )::bigint AS empty_cells,
                count(*) FILTER (
                    WHERE geom IS NOT NULL AND NOT ST_IsValid(geom)
                )::bigint AS invalid_cells
            FROM grid_cells
            WHERE region_id = :region_id
              AND grid_version = :grid_version
            """
        ),
        {"region_id": region_id, "grid_version": grid_version},
    ).mappings().one()


def _tile_metrics(
    db,
    *,
    region_id: int,
    grid_version: str,
    mask_version: str,
    projected_srid: int,
    tile,
):
    return db.execute(
        text(
            """
            WITH tile AS (
                SELECT
                    ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, :projected_srid) AS geom_projected,
                    ST_Transform(
                        ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, :projected_srid),
                        4326
                    ) AS geom_4326
            ),
            mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_UnaryUnion(
                            ST_Collect(ST_Intersection(component.geom_projected, tile.geom_projected))
                        ),
                        3
                    )
                ) AS geom
                FROM stage3_region_mask_components component, tile
                WHERE component.region_id = :region_id
                  AND component.mask_version = :mask_version
                  AND component.projected_srid = :projected_srid
                  AND component.geom_projected && tile.geom_projected
                  AND ST_Intersects(component.geom_projected, tile.geom_projected)
            ),
            clipped_cells AS (
                SELECT
                    cell.id,
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_Intersection(
                                ST_Transform(cell.geom, :projected_srid),
                                tile.geom_projected
                            )
                        ),
                        3
                    ) AS geom
                FROM grid_cells cell, tile
                WHERE cell.region_id = :region_id
                  AND cell.grid_version = :grid_version
                  AND cell.geom && tile.geom_4326
                  AND ST_Intersects(cell.geom, tile.geom_4326)
                  AND ST_Intersects(ST_Transform(cell.geom, :projected_srid), tile.geom_projected)
            ),
            cells AS (
                SELECT
                    id,
                    geom
                FROM clipped_cells
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            ),
            cell_metrics AS (
                SELECT
                    count(*)::bigint AS cell_count,
                    COALESCE(sum(ST_Area(geom)), 0)::float AS cell_area_m2
                FROM cells
            ),
            cell_union AS (
                SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM cells
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            )
            SELECT
                cell_metrics.cell_count,
                GREATEST(
                    cell_metrics.cell_area_m2 - COALESCE(ST_Area(cell_union.geom), 0),
                    0
                )::float AS cell_overlap_m2,
                COALESCE(
                    ST_Area(
                        CASE
                            WHEN mask.geom IS NULL THEN NULL
                            WHEN cell_union.geom IS NULL THEN mask.geom
                            ELSE ST_Difference(mask.geom, cell_union.geom)
                        END
                    ),
                    0
                )::float AS mask_without_cells_m2,
                COALESCE(
                    ST_Area(
                        CASE
                            WHEN cell_union.geom IS NULL THEN NULL
                            WHEN mask.geom IS NULL THEN cell_union.geom
                            ELSE ST_Difference(cell_union.geom, mask.geom)
                        END
                    ),
                    0
                )::float AS cells_outside_mask_m2
            FROM cell_metrics
            CROSS JOIN cell_union
            CROSS JOIN mask
            """
        ),
        {
            "region_id": region_id,
            "grid_version": grid_version,
            "mask_version": mask_version,
            "projected_srid": projected_srid,
            "min_x": float(tile["min_x"]),
            "min_y": float(tile["min_y"]),
            "max_x": float(tile["max_x"]),
            "max_y": float(tile["max_y"]),
        },
    ).mappings().one()


def _empty_report(region_code: str, grid_version: str, mask_version: str, projected_srid: int, reason: str) -> dict[str, Any]:
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "mask_version": mask_version,
        "projected_srid": projected_srid,
        "blocker": reason,
        "checks": {"ready": False},
    }


if __name__ == "__main__":
    main()
