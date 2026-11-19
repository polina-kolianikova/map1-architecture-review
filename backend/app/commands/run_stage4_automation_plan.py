from __future__ import annotations

import argparse
import json
from datetime import date

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import (
    active_stage4_run,
    build_stage4_plan,
    create_stage4_run,
    default_update_period,
    enabled_task_names,
    load_queued_batch_ids,
    require_stage4_provider_preflight,
)


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    region_code = args.region_code or settings.stage4_region_code
    grid_version = args.grid_version or settings.stage4_grid_version
    task_names = _task_names(args, settings)
    requested_start, requested_end = _requested_period(args)

    with SessionLocal() as db:
        plan = build_stage4_plan(
            db,
            region_code=region_code,
            grid_version=grid_version,
            batch_size=args.batch_size or settings.stage4_batch_size,
            task_names=task_names,
            max_cells=args.max_cells,
        )
        if not args.apply:
            print(json.dumps(plan, indent=2, sort_keys=True, default=str))
            return
        if plan["blockers"]:
            raise SystemExit("Stage 4 plan has blockers: " + "; ".join(plan["blockers"]))
        active_run = active_stage4_run(db, region_code=region_code, grid_version=grid_version)
        if active_run and args.max_cells is None:
            raise SystemExit(
                "Active Stage 4 run already exists: "
                f"{active_run['run_uuid']} ({active_run['status']}). "
                "Resume queued or failed batches instead of creating a duplicate full-scope run."
            )
        if active_run and not args.force_new_run:
            raise SystemExit(
                "Active Stage 4 run already exists: "
                f"{active_run['run_uuid']} ({active_run['status']}). "
                "Use --force-new-run only for capped diagnostic runs."
            )
        provider_preflight = (
            require_stage4_provider_preflight(settings)
            if args.max_cells is None
            else {"checked": False, "reason": "capped_diagnostic_run"}
        )
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
            batch_size=args.batch_size or settings.stage4_batch_size,
            task_names=task_names,
            max_cells=args.max_cells,
            metadata={"command": "run_stage4_automation_plan"},
        )
        batch_ids = load_queued_batch_ids(db, int(run["id"]))
        db.commit()

    queued_batches = 0
    if args.enqueue:
        from app.tasks.stage4 import enqueue_batches

        queued_batches = enqueue_batches(batch_ids)
    print(
        json.dumps(
            {
                "applied": True,
                "enqueued": args.enqueue,
                "queued_batches": queued_batches,
                "run_id": int(run["id"]),
                "run_uuid": str(run["run_uuid"]),
                "region_code": region_code,
                "grid_version": grid_version,
                "tasks": task_names,
                "total_batches": len(batch_ids),
                "max_cells": args.max_cells,
                "provider_preflight": provider_preflight,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _parse_args():
    parser = argparse.ArgumentParser(description="Plan or create a Stage 4 Layer B automation run")
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
    parser.add_argument("--ndvi-only", action="store_true")
    parser.add_argument("--biomass-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--force-new-run", action="store_true")
    parser.add_argument("--trigger-source", default="manual")
    args = parser.parse_args()
    if args.ndvi_only and args.biomass_only:
        raise ValueError("--ndvi-only cannot be combined with --biomass-only")
    if args.enqueue and not args.apply:
        raise ValueError("--enqueue requires --apply")
    return args


def _task_names(args, settings) -> list[str]:
    if args.ndvi_only:
        return ["ndvi_update"]
    if args.biomass_only:
        return ["biomass_update"]
    return enabled_task_names(settings)


def _requested_period(args) -> tuple[date, date]:
    if args.start or args.end:
        if not args.start or not args.end:
            raise ValueError("--start and --end must be provided together")
        return date.fromisoformat(args.start), date.fromisoformat(args.end)
    return default_update_period(args.lookback_days)


if __name__ == "__main__":
    main()
