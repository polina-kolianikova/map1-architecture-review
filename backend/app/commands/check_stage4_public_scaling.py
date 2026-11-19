from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.commands.check_stage4_automation import _latest_public_raster_zonal_proof
from app.db.session import SessionLocal
from app.services.stage4_automation import (
    PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER,
    build_stage4_plan,
    enabled_task_names,
    load_scope,
)
from app.services.stage4_public_sources import public_source_plan
from app.services.stage4_providers import stage4_provider_capabilities


def main() -> None:
    parser = argparse.ArgumentParser(description="Check public Stage 4 scaling against the contract cadence")
    parser.add_argument("--region-code")
    parser.add_argument("--grid-version")
    parser.add_argument("--fail-if-unfit", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    region_code = args.region_code or settings.stage4_region_code
    grid_version = args.grid_version or settings.stage4_grid_version
    capabilities = stage4_provider_capabilities(settings)

    with SessionLocal() as db:
        scope = load_scope(db, region_code, grid_version)
        latest_probe = _latest_public_statistics_probe(db, region_code, grid_version)
        metric_statuses = _metric_statuses(db, int(latest_probe["id"])) if latest_probe else []
        raster_zonal_proof = (
            _latest_public_raster_zonal_proof(db, region_code, grid_version)
            if capabilities.adapter == "public_raster_zonal"
            else None
        )
        raster_zonal_plan = (
            build_stage4_plan(
                db,
                region_code=region_code,
                grid_version=grid_version,
                batch_size=settings.stage4_batch_size,
                task_names=enabled_task_names(settings),
            )
            if capabilities.adapter == "public_raster_zonal"
            else None
        )
        raster_zonal_timing = (
            _latest_public_raster_zonal_timing(db, region_code, grid_version)
            if capabilities.adapter == "public_raster_zonal"
            else None
        )
        source_plan = public_source_plan(settings) if capabilities.adapter == "public_raster_zonal" else None

    report = _scaling_report(
        scope_total_cells=scope.total_cells,
        cadence_days=settings.stage4_update_cadence_days,
        hard_ceiling_cells=settings.stage4_public_statistics_hard_ceiling_cells,
        active_provider_adapter=capabilities.adapter,
        latest_probe=latest_probe,
        metric_statuses=metric_statuses,
        raster_zonal_proof=raster_zonal_proof,
        raster_zonal_plan=raster_zonal_plan,
        raster_zonal_timing=raster_zonal_timing,
        public_source_plan_info=source_plan,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.fail_if_unfit and not report["ready_for_full_scope"]:
        raise SystemExit("Stage 4 public scaling is not ready for full scope")


def _scaling_report(
    *,
    scope_total_cells: int,
    cadence_days: int,
    hard_ceiling_cells: int,
    active_provider_adapter: str,
    latest_probe: dict[str, Any] | None,
    metric_statuses: list[dict[str, Any]],
    raster_zonal_proof: dict[str, Any] | None = None,
    raster_zonal_plan: dict[str, Any] | None = None,
    raster_zonal_timing: dict[str, Any] | None = None,
    public_source_plan_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blockers = []
    throughput = None
    estimated_full_scope_days = None
    ndvi_ok_ratio = None
    if active_provider_adapter == "public_raster_zonal":
        if raster_zonal_plan is None:
            blockers.append("Public raster-zonal full-scope plan is not available")
        else:
            blockers.extend(str(blocker) for blocker in raster_zonal_plan.get("blockers", []))
        if public_source_plan_info is not None:
            blockers.extend(
                str(blocker)
                for blocker in public_source_plan_info.get("blockers", [])
                if blocker != "Shared public raster cache is disabled"
            )
            if not public_source_plan_info.get("shared_raster_cache_enabled") and scope_total_cells > hard_ceiling_cells:
                blockers.append("Shared public raster cache is required before full-scope public raster-zonal runs")
        if not raster_zonal_proof or not raster_zonal_proof.get("ok"):
            blockers.append("Public raster-zonal Stage 4 requires a successful small tile proof before full-scope readiness")
        raster_zonal_throughput, raster_zonal_estimated_days = _raster_zonal_throughput(
            scope_total_cells=scope_total_cells,
            timing=raster_zonal_timing,
        )
        if raster_zonal_throughput is None or raster_zonal_estimated_days is None:
            blockers.append("Public raster-zonal Stage 4 requires observed throughput before full-scope readiness")
        elif raster_zonal_estimated_days > cadence_days:
            blockers.append("Public raster-zonal observed throughput does not fit the contract cadence")
        if raster_zonal_timing and int(raster_zonal_timing.get("failed_batches") or 0) > 0:
            blockers.append("Public raster-zonal Stage 4 has failed batches that must be repaired before full-scope readiness")
        if raster_zonal_timing and int(raster_zonal_timing.get("open_alerts") or 0) > 0:
            blockers.append("Public raster-zonal Stage 4 has open alerts that must be resolved before full-scope readiness")
        return {
            "stage": "stage4",
            "scope": "public_scaling",
            "ready_for_full_scope": not blockers,
            "active_provider_adapter": active_provider_adapter,
            "recommended_provider_adapter": "public_raster_zonal",
            "scope_total_cells": scope_total_cells,
            "contract_cadence_days": cadence_days,
            "public_statistics_hard_ceiling_cells": hard_ceiling_cells,
            "latest_public_statistics_probe": _probe_payload(latest_probe),
            "public_statistics_throughput": throughput,
            "public_statistics_estimated_full_scope_days": estimated_full_scope_days,
            "public_statistics_ndvi_ok_ratio": ndvi_ok_ratio,
            "public_raster_zonal_proof": raster_zonal_proof,
            "public_raster_zonal_plan": _raster_zonal_plan_payload(raster_zonal_plan),
            "public_source_plan": public_source_plan_info,
            "public_raster_zonal_timing": raster_zonal_timing,
            "public_raster_zonal_throughput": raster_zonal_throughput,
            "public_raster_zonal_estimated_full_scope_days": raster_zonal_estimated_days,
            "metric_statuses": metric_statuses,
            "blockers": sorted(set(blockers)),
        }

    if latest_probe is None:
        blockers.append("No completed public statistics scaling probe is available")
    else:
        duration_seconds = float(latest_probe["ndvi_duration_seconds"] or 0)
        probe_cells = int(latest_probe["total_cells"] or 0)
        if duration_seconds <= 0 or probe_cells <= 0:
            blockers.append("Latest public statistics probe does not have usable timing")
        else:
            throughput = {
                "cells_per_second": round(probe_cells / duration_seconds, 6),
                "cells_per_minute": round((probe_cells / duration_seconds) * 60, 4),
                "cells_per_hour": round((probe_cells / duration_seconds) * 3600, 2),
            }
            estimated_full_scope_days = round(scope_total_cells / (probe_cells / duration_seconds) / 86400, 2)
            if estimated_full_scope_days > cadence_days:
                blockers.append(
                    "Public statistics observed throughput does not fit the contract cadence"
                )
        ndvi_ok_cells = _metric_cells(metric_statuses, metric_name="ndvi", source_status="ok")
        ndvi_total_cells = sum(int(row["cells"] or 0) for row in metric_statuses if row["metric_name"] == "ndvi")
        ndvi_ok_ratio = round(ndvi_ok_cells / ndvi_total_cells, 5) if ndvi_total_cells else 0
        if ndvi_total_cells < probe_cells:
            blockers.append("Latest public statistics probe is missing NDVI metric rows")

    if active_provider_adapter == "cdse_public_statistics" and scope_total_cells > hard_ceiling_cells:
        blockers.append(PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER)

    return {
        "stage": "stage4",
        "scope": "public_scaling",
        "ready_for_full_scope": not blockers,
        "active_provider_adapter": active_provider_adapter,
        "recommended_provider_adapter": "public_raster_zonal",
        "scope_total_cells": scope_total_cells,
        "contract_cadence_days": cadence_days,
        "public_statistics_hard_ceiling_cells": hard_ceiling_cells,
        "latest_public_statistics_probe": _probe_payload(latest_probe),
        "public_statistics_throughput": throughput,
        "public_statistics_estimated_full_scope_days": estimated_full_scope_days,
        "public_statistics_ndvi_ok_ratio": ndvi_ok_ratio,
        "public_raster_zonal_proof": raster_zonal_proof,
        "public_raster_zonal_plan": _raster_zonal_plan_payload(raster_zonal_plan),
        "public_source_plan": public_source_plan_info,
        "public_raster_zonal_timing": raster_zonal_timing,
        "public_raster_zonal_throughput": None,
        "public_raster_zonal_estimated_full_scope_days": None,
        "metric_statuses": metric_statuses,
        "blockers": sorted(set(blockers)),
    }


def _raster_zonal_throughput(
    *,
    scope_total_cells: int,
    timing: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, float | None]:
    if timing is None:
        return None, None
    duration_seconds = float(timing.get("ndvi_duration_seconds") or 0)
    processed_cells = int(timing.get("processed_cells") or 0)
    completed_batches = int(timing.get("completed_batches") or 0)
    if duration_seconds <= 0 or processed_cells <= 0 or completed_batches < 10:
        return None, None
    cells_per_second = processed_cells / duration_seconds
    throughput = {
        "cells_per_second": round(cells_per_second, 6),
        "cells_per_minute": round(cells_per_second * 60, 4),
        "cells_per_hour": round(cells_per_second * 3600, 2),
        "completed_batches": completed_batches,
    }
    return throughput, round(scope_total_cells / cells_per_second / 86400, 4)


def _latest_public_statistics_probe(db, region_code: str, grid_version: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            WITH candidate_runs AS (
                SELECT run.*
                FROM stage4_layer_b_runs run
                WHERE run.region_code = :region_code
                  AND run.grid_version = :grid_version
                  AND run.metadata->>'command' = 'run_stage4_public_proof'
                  AND run.metadata->>'provider_adapter' = 'cdse_public_statistics'
                  AND COALESCE((run.metadata->>'proof')::boolean, false)
                  AND run.status IN ('completed', 'completed_with_errors')
                  AND run.started_at IS NOT NULL
                  AND run.finished_at IS NOT NULL
                  AND run.failed_cells = 0
            ),
            ndvi_timing AS (
                SELECT
                    batch.run_id,
                    EXTRACT(EPOCH FROM max(batch.finished_at) - min(batch.started_at)) AS ndvi_duration_seconds
                FROM stage4_layer_b_batches batch
                JOIN candidate_runs run ON run.id = batch.run_id
                WHERE batch.task_name = 'ndvi_update'
                  AND batch.started_at IS NOT NULL
                  AND batch.finished_at IS NOT NULL
                GROUP BY batch.run_id
            )
            SELECT
                run.id,
                run.run_uuid::text AS run_uuid,
                run.status,
                run.total_cells,
                run.succeeded_cells,
                run.skipped_cells,
                run.requested_start,
                run.requested_end,
                run.max_cloud_coverage,
                run.min_valid_pixel_ratio,
                run.started_at,
                run.finished_at,
                ndvi_timing.ndvi_duration_seconds
            FROM candidate_runs run
            JOIN ndvi_timing ON ndvi_timing.run_id = run.id
            ORDER BY run.total_cells DESC, run.finished_at DESC, run.id DESC
            LIMIT 1
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _latest_public_raster_zonal_timing(db, region_code: str, grid_version: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            WITH candidate_runs AS (
                SELECT run.*
                FROM stage4_layer_b_runs run
                WHERE run.region_code = :region_code
                  AND run.grid_version = :grid_version
                  AND run.metadata->>'provider_adapter' = 'public_raster_zonal'
                  AND run.status IN (
                      'queued',
                      'running',
                      'completed',
                      'completed_with_errors'
                  )
                ORDER BY COALESCE(run.finished_at, run.updated_at, run.created_at) DESC, run.id DESC
                LIMIT 1
            ),
            completed_ndvi_batches AS (
                SELECT
                    batch.*,
                    row_number() OVER (ORDER BY batch.finished_at DESC, batch.id DESC) AS recency_rank
                FROM stage4_layer_b_batches batch
                JOIN candidate_runs run ON run.id = batch.run_id
                WHERE batch.task_name = 'ndvi_update'
                  AND batch.status IN ('completed', 'completed_with_errors')
                  AND batch.started_at IS NOT NULL
                  AND batch.finished_at IS NOT NULL
                  AND (
                      run.finished_at IS NULL
                      OR batch.finished_at <= run.finished_at
                  )
            ),
            recent_ndvi_timing AS (
                SELECT
                    run_id,
                    count(*) AS completed_batches,
                    COALESCE(sum(processed_cells), 0)::bigint AS processed_cells,
                    min(started_at) AS first_started_at,
                    max(finished_at) AS last_finished_at
                FROM completed_ndvi_batches
                WHERE recency_rank <= 50
                GROUP BY run_id
            ),
            all_ndvi_timing AS (
                SELECT
                    batch.run_id,
                    count(*) FILTER (WHERE batch.status IN ('completed', 'completed_with_errors')) AS total_completed_batches,
                    count(*) FILTER (WHERE batch.status = 'failed') AS failed_batches,
                    COALESCE(sum(batch.processed_cells) FILTER (
                        WHERE batch.status IN ('completed', 'completed_with_errors')
                    ), 0)::bigint AS total_processed_cells,
                    min(batch.started_at) FILTER (
                        WHERE batch.status IN ('completed', 'completed_with_errors')
                          AND batch.started_at IS NOT NULL
                    ) AS full_first_started_at,
                    max(batch.finished_at) FILTER (
                        WHERE batch.status IN ('completed', 'completed_with_errors')
                          AND batch.finished_at IS NOT NULL
                    ) AS full_last_finished_at
                FROM stage4_layer_b_batches batch
                JOIN candidate_runs run ON run.id = batch.run_id
                WHERE batch.task_name = 'ndvi_update'
                GROUP BY batch.run_id
            ),
            alert_counts AS (
                SELECT
                    alert.run_id,
                    count(*) FILTER (WHERE alert.status <> 'resolved') AS open_alerts
                FROM stage4_alerts alert
                JOIN candidate_runs run ON run.id = alert.run_id
                GROUP BY alert.run_id
            )
            SELECT
                run.id,
                run.run_uuid::text AS run_uuid,
                run.status,
                run.total_cells,
                run.processed_cells AS run_processed_cells,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN 'full_completed_run'
                    ELSE 'recent_completed_batches'
                END AS sample_scope,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN NULL
                    ELSE 50
                END::integer AS sample_batch_limit,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN COALESCE(all_ndvi_timing.total_completed_batches, 0)
                    ELSE COALESCE(recent_ndvi_timing.completed_batches, 0)
                END::integer AS completed_batches,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN COALESCE(all_ndvi_timing.total_processed_cells, 0)
                    ELSE COALESCE(recent_ndvi_timing.processed_cells, 0)
                END::bigint AS processed_cells,
                COALESCE(all_ndvi_timing.total_completed_batches, 0)::integer AS total_completed_batches,
                COALESCE(all_ndvi_timing.total_processed_cells, 0)::bigint AS total_processed_cells,
                COALESCE(all_ndvi_timing.failed_batches, 0)::integer AS failed_batches,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN all_ndvi_timing.full_first_started_at
                    ELSE recent_ndvi_timing.first_started_at
                END AS first_started_at,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN all_ndvi_timing.full_last_finished_at
                    ELSE recent_ndvi_timing.last_finished_at
                END AS last_finished_at,
                CASE
                    WHEN run.status IN ('completed', 'completed_with_errors')
                         AND all_ndvi_timing.full_first_started_at IS NOT NULL
                         AND all_ndvi_timing.full_last_finished_at IS NOT NULL
                    THEN EXTRACT(EPOCH FROM all_ndvi_timing.full_last_finished_at - all_ndvi_timing.full_first_started_at)
                    WHEN recent_ndvi_timing.first_started_at IS NULL
                         OR recent_ndvi_timing.last_finished_at IS NULL
                    THEN NULL
                    ELSE EXTRACT(EPOCH FROM recent_ndvi_timing.last_finished_at - recent_ndvi_timing.first_started_at)
                END AS ndvi_duration_seconds,
                COALESCE(alert_counts.open_alerts, 0)::integer AS open_alerts
            FROM candidate_runs run
            LEFT JOIN recent_ndvi_timing ON recent_ndvi_timing.run_id = run.id
            LEFT JOIN all_ndvi_timing ON all_ndvi_timing.run_id = run.id
            LEFT JOIN alert_counts ON alert_counts.run_id = run.id
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _metric_statuses(db, run_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
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
            ORDER BY metric_name, source_status
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _metric_cells(rows: list[dict[str, Any]], *, metric_name: str, source_status: str) -> int:
    return sum(
        int(row["cells"] or 0)
        for row in rows
        if row["metric_name"] == metric_name and row["source_status"] == source_status
    )


def _probe_payload(probe: dict[str, Any] | None) -> dict[str, Any] | None:
    if probe is None:
        return None
    return {key: value for key, value in probe.items() if key != "id"}


def _raster_zonal_plan_payload(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    keys = (
        "provider_adapter",
        "ndvi_processing_mode",
        "supports_full_scope",
        "diagnostic_only",
        "batching_strategy",
        "batch_size",
        "requested_batch_size",
        "total_cells",
        "total_batches",
        "batches_per_task",
        "spatial_tile_count",
        "spatial_tile_cells",
        "spatial_tile_max_cells",
        "spatial_tile_run_id",
        "spatial_tile_run_uuid",
        "blockers",
    )
    return {key: plan.get(key) for key in keys if key in plan}


if __name__ == "__main__":
    main()
