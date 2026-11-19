from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import TASK_NDVI_UPDATE


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare public raster-zonal inputs for Stage 4 spatial batches")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-uuid", default="")
    parser.add_argument("--batch-id", type=int)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if not args.run_id and not args.run_uuid.strip():
        raise ValueError("--run-id or --run-uuid is required")
    if args.limit < 0:
        raise ValueError("--limit must not be negative")

    settings = get_settings()
    output_root = Path(args.output_dir or settings.stage4_public_raster_zonal_results_dir)
    with SessionLocal() as db:
        run = _load_run(db, run_id=args.run_id, run_uuid=args.run_uuid)
        batches = _load_batches(db, run_id=int(run["id"]), batch_id=args.batch_id, limit=args.limit)

    prepared = []
    for batch in batches:
        batch_dir = output_root / str(run["run_uuid"]) / f"batch-{int(batch['batch_index']):05d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        gpkg_path = batch_dir / "cells.gpkg"
        manifest_path = batch_dir / "manifest.json"
        if not args.overwrite and (gpkg_path.exists() or manifest_path.exists()):
            raise RuntimeError(f"Prepared raster-zonal inputs already exist for batch {batch['id']}")
        _export_batch_cells_gpkg(run, batch, gpkg_path, settings)
        manifest = _manifest(run, batch, gpkg_path)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
        prepared.append(
            {
                "batch_id": int(batch["id"]),
                "batch_index": int(batch["batch_index"]),
                "expected_cells": int(batch["expected_cells"]),
                "cells_gpkg": str(gpkg_path),
                "manifest": str(manifest_path),
                "result_csv": manifest["result_csv"],
            }
        )

    print(
        json.dumps(
            {
                "prepared": len(prepared),
                "run_id": int(run["id"]),
                "run_uuid": str(run["run_uuid"]),
                "output_dir": str(output_root),
                "batches": prepared,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _load_run(db, *, run_id: int | None, run_uuid: str) -> dict[str, Any]:
    clauses = []
    params: dict[str, Any] = {}
    if run_id:
        clauses.append("id = :run_id")
        params["run_id"] = run_id
    if run_uuid.strip():
        clauses.append("run_uuid = CAST(:run_uuid AS uuid)")
        params["run_uuid"] = run_uuid.strip()
    row = db.execute(
        text(
            f"""
            SELECT *
            FROM stage4_layer_b_runs
            WHERE {' OR '.join(clauses)}
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        params,
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("Stage 4 run was not found")
    return dict(row)


def _load_batches(db, *, run_id: int, batch_id: int | None, limit: int) -> list[dict[str, Any]]:
    clauses = [
        "run_id = :run_id",
        "task_name = :task_name",
        "metadata->>'batching_strategy' = 'stage3_spatial_tiles'",
    ]
    params: dict[str, Any] = {"run_id": run_id, "task_name": TASK_NDVI_UPDATE}
    if batch_id:
        clauses.append("id = :batch_id")
        params["batch_id"] = batch_id
    limit_sql = "LIMIT :limit" if limit else ""
    if limit:
        params["limit"] = limit
    rows = db.execute(
        text(
            f"""
            SELECT *
            FROM stage4_layer_b_batches
            WHERE {' AND '.join(clauses)}
            ORDER BY batch_index
            {limit_sql}
            """
        ),
        params,
    ).mappings().all()
    batches = [dict(row) for row in rows]
    if not batches:
        raise RuntimeError("No public raster-zonal Stage 4 NDVI batches were found")
    return batches


def _export_batch_cells_gpkg(
    run: dict[str, Any],
    batch: dict[str, Any],
    output_path: Path,
    settings,
) -> None:
    metadata = batch.get("metadata") if isinstance(batch.get("metadata"), dict) else {}
    bbox = metadata.get("bbox_projected")
    projected_srid = metadata.get("projected_srid")
    stage3_grid_run_id = metadata.get("stage3_grid_run_id")
    stage3_grid_tile_id = metadata.get("stage3_grid_tile_id")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or projected_srid is None
        or stage3_grid_run_id is None
        or stage3_grid_tile_id is None
    ):
        raise RuntimeError(f"Stage 4 spatial batch metadata is incomplete: {batch['id']}")
    output_path.unlink(missing_ok=True)
    sql = f"""
        WITH tile AS (
            SELECT
                ST_MakeEnvelope({float(bbox[0])}, {float(bbox[1])}, {float(bbox[2])}, {float(bbox[3])}, {int(projected_srid)})
                    AS geom_projected,
                ST_Transform(
                    ST_MakeEnvelope({float(bbox[0])}, {float(bbox[1])}, {float(bbox[2])}, {float(bbox[3])}, {int(projected_srid)}),
                    4326
                ) AS geom_4326
        )
        SELECT
            cell.id,
            cell.id::text AS identifier,
            cell.uuid::text AS cell_uuid,
            COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
            cell.geom
        FROM grid_cells cell, tile
        WHERE cell.region_id = {int(run["region_id"])}
          AND cell.grid_version = '{_sql_literal(str(run["grid_version"]))}'
          AND cell.id BETWEEN {int(batch["grid_cell_id_start"])} AND {int(batch["grid_cell_id_end"])}
          AND cell.geom && tile.geom_4326
          AND ST_Covers(
                tile.geom_projected,
                ST_PointOnSurface(ST_Transform(cell.geom, {int(projected_srid)}))
              )
          AND NOT EXISTS (
                SELECT 1
                FROM stage3_grid_generation_tiles earlier_tile
                WHERE earlier_tile.run_id = {int(stage3_grid_run_id)}
                  AND earlier_tile.id < {int(stage3_grid_tile_id)}
                  AND earlier_tile.status = 'completed'
                  AND ST_Covers(
                        ST_MakeEnvelope(
                            earlier_tile.min_x,
                            earlier_tile.min_y,
                            earlier_tile.max_x,
                            earlier_tile.max_y,
                            {int(projected_srid)}
                        ),
                        ST_PointOnSurface(ST_Transform(cell.geom, {int(projected_srid)}))
                      )
              )
        ORDER BY cell.id
    """
    env = os.environ.copy()
    env["PGPASSWORD"] = settings.postgres_password
    dsn = (
        f"PG:host={settings.postgres_host} "
        f"port={settings.postgres_port} "
        f"dbname={settings.postgres_db} "
        f"user={settings.postgres_user}"
    )
    completed = subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            str(output_path),
            dsn,
            "-sql",
            sql,
            "-nln",
            "cells",
            "-nlt",
            "PROMOTE_TO_MULTI",
            "-lco",
            "SPATIAL_INDEX=YES",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"GeoPackage export failed: {completed.stderr[-1000:]}")


def _manifest(run: dict[str, Any], batch: dict[str, Any], gpkg_path: Path) -> dict[str, Any]:
    metadata = batch.get("metadata") if isinstance(batch.get("metadata"), dict) else {}
    batch_name = f"batch-{int(batch['batch_index']):05d}.csv"
    result_csv = gpkg_path.parent.parent / batch_name
    return {
        "stage": "stage4",
        "adapter": "public_raster_zonal",
        "run_id": int(run["id"]),
        "run_uuid": str(run["run_uuid"]),
        "batch_id": int(batch["id"]),
        "batch_index": int(batch["batch_index"]),
        "region_code": str(run["region_code"]),
        "grid_version": str(run["grid_version"]),
        "requested_start": str(run["requested_start"]),
        "requested_end": str(run["requested_end"]),
        "max_cloud_coverage": int(run["max_cloud_coverage"]),
        "min_valid_pixel_ratio": float(run["min_valid_pixel_ratio"]),
        "resolution": float(run["resolution"]),
        "expected_cells": int(batch["expected_cells"]),
        "cells_gpkg": str(gpkg_path),
        "result_csv": str(result_csv),
        "bbox_projected": metadata.get("bbox_projected"),
        "projected_srid": metadata.get("projected_srid"),
        "stage3_grid_tile_key": metadata.get("stage3_grid_tile_key"),
    }


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


if __name__ == "__main__":
    main()
