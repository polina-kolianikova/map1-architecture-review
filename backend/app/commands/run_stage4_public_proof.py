from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import (
    TASK_BIOMASS_UPDATE,
    TASK_NDVI_UPDATE,
    build_stage4_plan,
    create_stage4_run,
    default_update_period,
    load_queued_batch_ids,
    process_batch,
)
from app.services.stage4_providers import stage4_provider_capabilities


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    capabilities = stage4_provider_capabilities(settings)
    if capabilities.processing_path != "public":
        raise SystemExit("Stage 4 public proof requires the public processing path")
    if capabilities.uses_external_jobs or capabilities.uses_object_storage_exchange:
        raise SystemExit("Stage 4 public proof cannot use external jobs or object-storage exchange")

    region_code = args.region_code or settings.stage4_region_code
    grid_version = args.grid_version or settings.stage4_grid_version
    max_cells = args.max_cells or settings.stage4_public_proof_max_cells
    if capabilities.adapter == "public_raster_zonal":
        proof_cap = settings.stage4_public_proof_max_cells
    else:
        proof_cap = min(settings.stage4_public_proof_max_cells, settings.stage4_public_statistics_max_cells_per_run)
    if max_cells > proof_cap:
        raise SystemExit(f"Stage 4 public proof is capped at {proof_cap} cells")
    requested_start, requested_end = _requested_period(args)
    task_names = [TASK_NDVI_UPDATE] if args.ndvi_only else [TASK_NDVI_UPDATE, TASK_BIOMASS_UPDATE]

    with SessionLocal() as db:
        plan = build_stage4_plan(
            db,
            region_code=region_code,
            grid_version=grid_version,
            batch_size=args.batch_size or max_cells,
            task_names=task_names,
            max_cells=max_cells,
        )
        if not args.apply:
            print(json.dumps({"proof": True, "plan": plan}, indent=2, sort_keys=True, default=str))
            return
        if plan["blockers"]:
            raise SystemExit("Stage 4 public proof has blockers: " + "; ".join(plan["blockers"]))
        run = create_stage4_run(
            db,
            region_code=region_code,
            grid_version=grid_version,
            trigger_source=args.trigger_source,
            requested_start=requested_start,
            requested_end=requested_end,
            cadence_days=args.cadence_days or settings.stage4_update_cadence_days,
            max_cloud_coverage=args.max_cloud or settings.stage4_max_cloud_coverage,
            min_valid_pixel_ratio=args.min_valid_pixel_ratio or settings.stage4_min_valid_pixel_ratio,
            resolution=args.resolution or settings.stage4_statistics_resolution,
            batch_size=args.batch_size or max_cells,
            task_names=task_names,
            max_cells=max_cells,
            metadata={
                "command": "run_stage4_public_proof",
                "proof": True,
                "processing_path": capabilities.processing_path,
                "provider_adapter": capabilities.adapter,
            },
        )
        batch_ids = load_queued_batch_ids(db, int(run["id"]))
        db.commit()

    processed = []
    if args.process_inline:
        for batch_id in batch_ids:
            processed.append(process_batch(batch_id, settings=settings))

    with SessionLocal() as db:
        summary = _run_summary(db, int(run["id"]))
        metric_counts = _metric_counts(db, int(run["id"]))
        acceptance = (
            _proof_acceptance_report(
                db,
                int(run["id"]),
                expected_cells=int(plan["total_cells"]),
                expect_biomass=not args.ndvi_only,
                min_ndvi_ok_ratio=args.min_ndvi_ok_ratio,
            )
            if args.process_inline
            else {"checked": False, "errors": []}
        )
        if acceptance["checked"]:
            accepted = not acceptance["errors"]
            _record_public_proof_acceptance(db, int(run["id"]), accepted=accepted, acceptance=acceptance)
            db.commit()
            summary = _run_summary(db, int(run["id"]))

    payload = {
        "proof": True,
        "applied": True,
        "processed_inline": args.process_inline,
        "accepted": not acceptance["errors"] if acceptance["checked"] else None,
        "acceptance": acceptance,
        "run": summary,
        "batch_results": processed,
        "metric_counts": metric_counts,
        "plan": plan,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if acceptance["errors"]:
        raise SystemExit(
            "Stage 4 public proof failed acceptance gate: "
            + "; ".join(acceptance["errors"])
        )


def _proof_acceptance_report(
    db,
    run_id: int,
    *,
    expected_cells: int,
    expect_biomass: bool,
    min_ndvi_ok_ratio: float = 1.0,
) -> dict[str, Any]:
    batch_rows = db.execute(
        text(
            """
            SELECT
                task_name,
                status,
                expected_cells,
                processed_cells,
                succeeded_cells,
                failed_cells,
                skipped_cells,
                latest_error
            FROM stage4_layer_b_batches
            WHERE run_id = :run_id
            ORDER BY task_name, batch_index
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    metric_rows = db.execute(
        text(
            """
            SELECT
                metric_name,
                source_status,
                count(*)::integer AS rows,
                count(DISTINCT grid_cell_id)::integer AS cells
            FROM stage4_layer_b_metrics
            WHERE run_id = :run_id
            GROUP BY metric_name, source_status
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    open_alerts = int(
        db.execute(
            text(
                """
                SELECT count(*)::integer
                FROM stage4_alerts
                WHERE run_id = :run_id
                  AND status <> 'resolved'
                """
            ),
            {"run_id": run_id},
        ).scalar_one()
        or 0
    )
    errors: list[str] = []
    ndvi_batches = [row for row in batch_rows if row["task_name"] == TASK_NDVI_UPDATE]
    if not ndvi_batches:
        errors.append("NDVI proof batch was not created")
    ndvi_expected = sum(int(row["expected_cells"] or 0) for row in ndvi_batches)
    ndvi_processed = sum(int(row["processed_cells"] or 0) for row in ndvi_batches)
    ndvi_succeeded = sum(int(row["succeeded_cells"] or 0) for row in ndvi_batches)
    ndvi_failed = sum(int(row["failed_cells"] or 0) for row in ndvi_batches)
    ndvi_skipped = sum(int(row["skipped_cells"] or 0) for row in ndvi_batches)
    for row in ndvi_batches:
        if row["status"] not in {"completed", "skipped"}:
            errors.append("NDVI proof batch did not complete")
    if ndvi_expected != expected_cells:
        errors.append("NDVI proof expected cell count changed")
    if ndvi_processed != expected_cells:
        errors.append("NDVI proof did not process every expected cell")
    if ndvi_failed != 0:
        errors.append("NDVI proof has failed cells")

    ndvi_ok_cells = _metric_cells(metric_rows, metric_name="ndvi", source_status="ok")
    ndvi_total_cells = sum(int(row["cells"] or 0) for row in metric_rows if row["metric_name"] == "ndvi")
    ndvi_ok_ratio = (ndvi_ok_cells / expected_cells) if expected_cells else 0.0
    if ndvi_total_cells != expected_cells:
        errors.append("NDVI proof metric rows do not match expected cells")
    if ndvi_ok_ratio < min_ndvi_ok_ratio:
        errors.append("NDVI proof did not meet the required public-data coverage ratio")
    if min_ndvi_ok_ratio >= 1.0 and ndvi_succeeded != expected_cells:
        errors.append("NDVI proof did not succeed for every expected cell")
    if min_ndvi_ok_ratio >= 1.0 and ndvi_skipped != 0:
        errors.append("NDVI proof has skipped cells")

    if expect_biomass:
        biomass_pending_cells = _metric_cells(metric_rows, metric_name="biomass", source_status="pending_formula")
        biomass_total_cells = sum(int(row["cells"] or 0) for row in metric_rows if row["metric_name"] == "biomass")
        if biomass_total_cells != expected_cells:
            errors.append("Biomass proof metric rows do not match expected cells")
        if biomass_pending_cells != expected_cells:
            errors.append("Biomass proof is not explicitly pending_formula for every expected cell")

    if open_alerts:
        errors.append("Stage 4 public proof left open alerts")

    return {
        "checked": True,
        "expected_cells": expected_cells,
        "min_ndvi_ok_ratio": min_ndvi_ok_ratio,
        "ndvi_ok_ratio": ndvi_ok_ratio,
        "ndvi_expected_cells": ndvi_expected,
        "ndvi_processed_cells": ndvi_processed,
        "ndvi_succeeded_cells": ndvi_succeeded,
        "ndvi_failed_cells": ndvi_failed,
        "ndvi_skipped_cells": ndvi_skipped,
        "open_alerts": open_alerts,
        "batch_statuses": [dict(row) for row in batch_rows],
        "metric_statuses": [dict(row) for row in metric_rows],
        "errors": sorted(set(errors)),
    }


def _metric_cells(rows: list[Any], *, metric_name: str, source_status: str) -> int:
    return sum(
        int(row["cells"] or 0)
        for row in rows
        if row["metric_name"] == metric_name and row["source_status"] == source_status
    )


def _parse_args():
    parser = argparse.ArgumentParser(description="Run a small Stage 4 public-stack proof")
    parser.add_argument("--region-code")
    parser.add_argument("--grid-version")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--cadence-days", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-cells", type=int)
    parser.add_argument("--max-cloud", type=int)
    parser.add_argument("--min-valid-pixel-ratio", type=float)
    parser.add_argument("--resolution", type=float)
    parser.add_argument("--min-ndvi-ok-ratio", type=float, default=1.0)
    parser.add_argument("--ndvi-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--process-inline", action="store_true")
    parser.add_argument("--trigger-source", default="manual-public-proof")
    args = parser.parse_args()
    if args.process_inline and not args.apply:
        raise ValueError("--process-inline requires --apply")
    if args.max_cells is not None and args.max_cells <= 0:
        raise ValueError("--max-cells must be greater than zero")
    if args.min_ndvi_ok_ratio < 0 or args.min_ndvi_ok_ratio > 1:
        raise ValueError("--min-ndvi-ok-ratio must be between 0 and 1")
    return args


def _requested_period(args) -> tuple[date, date]:
    if args.start or args.end:
        if not args.start or not args.end:
            raise ValueError("--start and --end must be provided together")
        return date.fromisoformat(args.start), date.fromisoformat(args.end)
    return default_update_period(args.lookback_days)


def _run_summary(db, run_id: int) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
                id,
                run_uuid::text AS run_uuid,
                status,
                total_cells,
                total_batches,
                processed_cells,
                succeeded_cells,
                failed_cells,
                skipped_cells,
                latest_error,
                metadata
            FROM stage4_layer_b_runs
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id},
    ).mappings().one()
    return dict(row)


def _metric_counts(db, run_id: int) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT metric_name, count(*)::integer AS total
            FROM stage4_layer_b_metrics
            WHERE run_id = :run_id
            GROUP BY metric_name
            ORDER BY metric_name
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    return {str(row["metric_name"]): int(row["total"]) for row in rows}


def _record_public_proof_acceptance(db, run_id: int, *, accepted: bool, acceptance: dict[str, Any]) -> None:
    db.execute(
        text(
            """
            UPDATE stage4_layer_b_runs
            SET metadata = metadata || CAST(:metadata AS jsonb),
                updated_at = now()
            WHERE id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "metadata": json.dumps(
                {
                    "accepted": accepted,
                    "acceptance": acceptance,
                },
                default=str,
            ),
        },
    )


if __name__ == "__main__":
    main()
