from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.commands import fetch_cdse_ndvi
from app.db.session import SessionLocal
from app.services.stage4_automation import (
    STAGE4_NDVI_PIPELINE_NAME,
    Stage4BatchResult,
    mirror_stage4_observations_to_partitioned,
    record_event,
    refresh_run_rollup,
    sync_latest_ndvi_metric,
)


def main() -> None:
    args = _parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    with SessionLocal() as db:
        batch, run = _load_batch_and_run(db, args.batch_id)
        imported = failed = skipped = 0
        latest_error = None
        for path in sorted(input_dir.rglob("*.json"))[: args.limit or None]:
            try:
                payload = json.loads(path.read_text())
                statistics = _statistics_payload(payload)
                cell_id = _cell_id(payload)
                if cell_id is None:
                    skipped += 1
                    continue
                cell = _load_cell(db, cell_id, run)
                inserted = fetch_cdse_ndvi.store_observations(
                    db,
                    [],
                    statistics,
                    cell_id=int(cell["id"]),
                    cell_uuid=str(cell["uuid"]),
                    region_code=str(run["region_code"]),
                    grid_version=str(run["grid_version"]),
                    max_cloud_coverage=int(run["max_cloud_coverage"]),
                    fallback_date=run["requested_end"],
                    min_valid_pixel_ratio=float(run["min_valid_pixel_ratio"]),
                    request_start=datetime.combine(run["requested_start"], datetime.min.time(), tzinfo=UTC),
                    request_end=datetime.combine(run["requested_end"], datetime.max.time(), tzinfo=UTC),
                    request_resolution=float(run["resolution"]),
                    history_mode=False,
                    pipeline_name=STAGE4_NDVI_PIPELINE_NAME,
                )
                if inserted:
                    mirror_stage4_observations_to_partitioned(
                        db,
                        grid_cell_id=int(cell["id"]),
                        region_code=str(run["region_code"]),
                        grid_version=str(run["grid_version"]),
                    )
                    sync_latest_ndvi_metric(db, run=run, batch=batch, cell=cell)
                    imported += 1
                else:
                    skipped += 1
            except (OSError, json.JSONDecodeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
                failed += 1
                latest_error = str(exc)[:500]

        status = "completed"
        if failed and imported:
            status = "completed_with_errors"
        elif failed and not imported:
            status = "failed"
        _finish_external_batch(
            db,
            int(batch["id"]),
            status,
            Stage4BatchResult(
                processed_cells=imported + failed + skipped,
                succeeded_cells=imported,
                failed_cells=failed,
                skipped_cells=skipped,
                latest_error=latest_error,
            ),
        )
        record_event(
            db,
            run_id=int(run["id"]),
            batch_id=int(batch["id"]),
            severity="info" if status == "completed" else "warning",
            event_type="batch_statistical_imported",
            message=f"Stage 4 Batch Statistical outputs imported for batch {batch['id']}",
            details={"input_dir": str(input_dir), "imported": imported, "failed": failed, "skipped": skipped},
        )
        refresh_run_rollup(db, int(run["id"]))
        db.commit()

    print(json.dumps({"batch_id": args.batch_id, "imported": imported, "failed": failed, "skipped": skipped}, indent=2))


def _parse_args():
    parser = argparse.ArgumentParser(description="Import Stage 4 Batch Statistical NDVI output JSON files")
    parser.add_argument("--batch-id", type=int, required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def _load_batch_and_run(db, batch_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    batch = db.execute(
        text("SELECT * FROM stage4_layer_b_batches WHERE id = :batch_id"),
        {"batch_id": batch_id},
    ).mappings().one()
    run = db.execute(
        text("SELECT * FROM stage4_layer_b_runs WHERE id = :run_id"),
        {"run_id": int(batch["run_id"])},
    ).mappings().one()
    return dict(batch), dict(run)


def _load_cell(db, cell_id: int, run: dict[str, Any]) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
                cell.id,
                cell.uuid::text AS uuid,
                COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient
            FROM grid_cells cell
            WHERE cell.id = :cell_id
              AND cell.region_id = :region_id
              AND cell.grid_version = :grid_version
            """
        ),
        {
            "cell_id": int(cell_id),
            "region_id": int(run["region_id"]),
            "grid_version": str(run["grid_version"]),
        },
    ).mappings().one()
    return dict(row)


def _statistics_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "data" in payload:
        return payload
    result = payload.get("result")
    if isinstance(result, dict) and "data" in result:
        return result
    return payload


def _cell_id(payload: dict[str, Any]) -> int | None:
    for candidate in (
        payload.get("identifier"),
        payload.get("id"),
        payload.get("featureId"),
        payload.get("properties", {}).get("identifier") if isinstance(payload.get("properties"), dict) else None,
    ):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _finish_external_batch(db, batch_id: int, status: str, result: Stage4BatchResult) -> None:
    db.execute(
        text(
            """
            UPDATE stage4_layer_b_batches
            SET status = :status,
                processed_cells = :processed_cells,
                succeeded_cells = :succeeded_cells,
                failed_cells = :failed_cells,
                skipped_cells = :skipped_cells,
                latest_error = :latest_error,
                finished_at = now(),
                heartbeat_at = now(),
                updated_at = now()
            WHERE id = :batch_id
            """
        ),
        {
            "batch_id": batch_id,
            "status": status,
            "processed_cells": result.processed_cells,
            "succeeded_cells": result.succeeded_cells,
            "failed_cells": result.failed_cells,
            "skipped_cells": result.skipped_cells,
            "latest_error": result.latest_error,
        },
    )


if __name__ == "__main__":
    main()
