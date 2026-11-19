from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import batch_ranges, load_scope


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    region_code = args.region_code or settings.stage4_region_code
    grid_version = args.grid_version or settings.stage4_grid_version
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        scope = load_scope(db, region_code, grid_version)

    ranges = batch_ranges(scope.min_cell_id, scope.max_cell_id, args.batch_size or settings.stage4_batch_size)
    if args.max_batches is not None:
        ranges = ranges[: args.max_batches]

    outputs = []
    for start_id, end_id in ranges:
        output_path = output_dir / _gpkg_name(region_code, grid_version, start_id, end_id)
        if output_path.exists() and not args.overwrite:
            outputs.append({"path": str(output_path), "start_id": start_id, "end_id": end_id, "status": "exists"})
            continue
        _export_gpkg(
            output_path=output_path,
            region_code=region_code,
            grid_version=grid_version,
            start_id=start_id,
            end_id=end_id,
            ogr2ogr_path=args.ogr2ogr_path,
        )
        outputs.append({"path": str(output_path), "start_id": start_id, "end_id": end_id, "status": "written"})

    print(
        json.dumps(
            {
                "region_code": region_code,
                "grid_version": grid_version,
                "total_cells": scope.total_cells,
                "batch_size": args.batch_size or settings.stage4_batch_size,
                "outputs": outputs,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _parse_args():
    parser = argparse.ArgumentParser(description="Export Stage 4 Batch Statistical GeoPackage inputs")
    parser.add_argument("--region-code")
    parser.add_argument("--grid-version")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--ogr2ogr-path", default="ogr2ogr")
    return parser.parse_args()


def _export_gpkg(
    *,
    output_path: Path,
    region_code: str,
    grid_version: str,
    start_id: int,
    end_id: int,
    ogr2ogr_path: str,
) -> None:
    settings = get_settings()
    sql = f"""
        SELECT
            cell.id,
            cell.id::text AS identifier,
            cell.uuid::text AS cell_uuid,
            COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
            cell.geom
        FROM grid_cells cell
        JOIN regions region ON region.id = cell.region_id
        WHERE region.code = '{_sql_literal(region_code)}'
          AND cell.grid_version = '{_sql_literal(grid_version)}'
          AND cell.id BETWEEN {int(start_id)} AND {int(end_id)}
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
    cmd = [
        ogr2ogr_path,
        "-f",
        "GPKG",
        str(output_path),
        dsn,
        "-sql",
        sql,
        "-nln",
        "features",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "SPATIAL_INDEX=YES",
    ]
    completed = subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"GeoPackage export failed: {completed.stderr[-1000:]}")


def _gpkg_name(region_code: str, grid_version: str, start_id: int, end_id: int) -> str:
    safe_region = _safe_name(region_code)
    safe_grid = _safe_name(grid_version)
    return f"{safe_region}-{safe_grid}-{start_id}-{end_id}.gpkg"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


if __name__ == "__main__":
    main()
