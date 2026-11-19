from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import build_stage4_plan, detect_stuck_batches, enabled_task_names
from app.services.stage4_providers import get_stage4_ndvi_provider, stage4_provider_capabilities


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Stage 4 Layer B automation readiness")
    parser.add_argument("--region-code")
    parser.add_argument("--grid-version")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--detect-stuck", action="store_true")
    parser.add_argument("--check-provider-permissions", action="store_true")
    parser.add_argument("--fail-on-blockers", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    capabilities = stage4_provider_capabilities(settings)
    region_code = args.region_code or settings.stage4_region_code
    grid_version = args.grid_version or settings.stage4_grid_version
    blockers = []
    with SessionLocal() as db:
        if args.detect_stuck:
            detect_stuck_batches(db, stuck_after_minutes=settings.stage4_stuck_after_minutes)
            db.commit()
        table_report = _table_report(db)
        missing_tables = [name for name, exists in table_report.items() if not exists]
        if missing_tables:
            blockers.append("missing Stage 4 tables: " + ", ".join(missing_tables))
        plan = build_stage4_plan(
            db,
            region_code=region_code,
            grid_version=grid_version,
            batch_size=args.batch_size or settings.stage4_batch_size,
            task_names=enabled_task_names(settings),
        )
        blockers.extend(plan["blockers"])
        provider_permissions = _provider_permission_report(settings) if args.check_provider_permissions else {
            "checked": False
        }
        provider_blocker = provider_permissions.get("blocker")
        if provider_blocker:
            blockers.append(str(provider_blocker))
        open_alerts = _open_alert_count(db, region_code, grid_version)
        if open_alerts:
            blockers.append(f"Stage 4 has {open_alerts} open alerts")
        active_run = _active_run(db, region_code, grid_version)
        if active_run:
            blockers.append(
                "Active Stage 4 run already exists: "
                f"{active_run['run_uuid']} ({active_run['status']})"
            )
        public_raster_zonal_proof = (
            _latest_public_raster_zonal_proof(db, region_code, grid_version)
            if capabilities.adapter == "public_raster_zonal"
            else {"required": False}
        )
        if capabilities.adapter == "public_raster_zonal" and not public_raster_zonal_proof.get("ok"):
            blockers.append("Public raster-zonal Stage 4 requires a successful small tile proof before full-scope readiness")
        report = {
            "stage": "stage4",
            "scope": "layer_b_automation",
            "ready": not blockers,
            "blockers": blockers,
            "tables": table_report,
            "plan": plan,
            "provider_permissions": provider_permissions,
            "public_raster_zonal_proof": public_raster_zonal_proof,
            "active_run": active_run,
            "latest_run": _latest_run(db, region_code, grid_version),
            "open_alerts": open_alerts,
        }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.fail_on_blockers and blockers:
        raise SystemExit("Stage 4 readiness has blockers")


def _table_report(db) -> dict[str, bool]:
    tables = (
        "stage4_layer_b_runs",
        "stage4_layer_b_batches",
        "stage4_layer_b_events",
        "stage4_layer_b_metrics",
        "stage4_satellite_artifacts",
        "stage4_process_heartbeats",
        "stage4_alerts",
    )
    return {
        table_name: bool(
            db.execute(
                text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
                {"table_name": table_name},
            ).scalar_one()
        )
        for table_name in tables
    }


def _latest_run(db, region_code: str, grid_version: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT
                run_uuid::text AS run_uuid,
                status,
                total_cells,
                total_batches,
                processed_cells,
                succeeded_cells,
                failed_cells,
                skipped_cells,
                latest_error,
                created_at,
                started_at,
                finished_at,
                updated_at
            FROM stage4_layer_b_runs
            WHERE region_code = :region_code
              AND grid_version = :grid_version
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _active_run(db, region_code: str, grid_version: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT
                run_uuid::text AS run_uuid,
                status,
                total_cells,
                total_batches,
                processed_cells,
                succeeded_cells,
                failed_cells,
                skipped_cells,
                latest_error,
                created_at,
                started_at,
                finished_at,
                updated_at
            FROM stage4_layer_b_runs
            WHERE region_code = :region_code
              AND grid_version = :grid_version
              AND status IN (
                  'planned',
                  'queued',
                  'running',
                  'external_wait',
                  'external_ready'
              )
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _open_alert_count(db, region_code: str, grid_version: str) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT count(*)::bigint
                FROM stage4_alerts alert
                LEFT JOIN stage4_layer_b_runs run ON run.id = alert.run_id
                WHERE alert.status <> 'resolved'
                  AND (
                      alert.run_id IS NULL
                      OR (
                          run.region_code = :region_code
                          AND run.grid_version = :grid_version
                      )
                  )
                """
            ),
            {"region_code": region_code, "grid_version": grid_version},
        ).scalar_one()
        or 0
    )


def _provider_permission_report(settings) -> dict[str, Any]:
    return get_stage4_ndvi_provider(settings).check_permissions(settings)


def _latest_public_raster_zonal_proof(db, region_code: str, grid_version: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH candidate_runs AS (
                SELECT run.*
                FROM stage4_layer_b_runs run
                WHERE run.region_code = :region_code
                  AND run.grid_version = :grid_version
                  AND run.metadata->>'command' = 'run_stage4_public_proof'
                  AND run.metadata->>'provider_adapter' = 'public_raster_zonal'
                  AND COALESCE((run.metadata->>'proof')::boolean, false)
                  AND COALESCE((run.metadata->>'accepted')::boolean, false)
                  AND COALESCE((run.metadata->'acceptance'->>'checked')::boolean, false)
                  AND jsonb_array_length(COALESCE(run.metadata->'acceptance'->'errors', '[]'::jsonb)) = 0
                  AND COALESCE((run.metadata->'acceptance'->>'ndvi_ok_ratio')::double precision, 0.0) >= 0.9
                  AND COALESCE((run.metadata->'acceptance'->>'min_ndvi_ok_ratio')::double precision, 1.0) >= 0.9
                  AND run.status IN ('completed', 'completed_with_errors')
                ORDER BY run.finished_at DESC NULLS LAST, run.updated_at DESC, run.id DESC
                LIMIT 1
            ),
            metric_counts AS (
                SELECT
                    metric.run_id,
                    count(DISTINCT metric.grid_cell_id) FILTER (WHERE metric.metric_name = 'ndvi') AS ndvi_metric_cells,
                    count(DISTINCT metric.grid_cell_id) FILTER (
                        WHERE metric.metric_name = 'ndvi'
                          AND metric.source_status = 'ok'
                    ) AS ndvi_ok_cells
                FROM stage4_layer_b_metrics metric
                JOIN candidate_runs run ON run.id = metric.run_id
                GROUP BY metric.run_id
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
                run.failed_cells,
                run.finished_at,
                COALESCE((run.metadata->'acceptance'->>'ndvi_ok_ratio')::double precision, 0.0) AS ndvi_ok_ratio,
                COALESCE((run.metadata->'acceptance'->>'min_ndvi_ok_ratio')::double precision, 1.0) AS min_ndvi_ok_ratio,
                COALESCE((run.metadata->'acceptance'->>'expected_cells')::integer, run.total_cells, 0) AS accepted_cells,
                COALESCE(metric_counts.ndvi_metric_cells, 0)::integer AS ndvi_metric_cells,
                COALESCE(metric_counts.ndvi_ok_cells, 0)::integer AS ndvi_ok_cells,
                COALESCE(alert_counts.open_alerts, 0)::integer AS open_alerts
            FROM candidate_runs run
            LEFT JOIN metric_counts ON metric_counts.run_id = run.id
            LEFT JOIN alert_counts ON alert_counts.run_id = run.id
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    if row is None:
        return {"required": True, "ok": False, "latest": None}
    payload = dict(row)
    payload["ok"] = (
        int(payload["failed_cells"] or 0) == 0
        and int(payload["accepted_cells"] or payload["total_cells"] or 0) > 0
        and float(payload["ndvi_ok_ratio"] or 0) >= 0.9
        and float(payload["min_ndvi_ok_ratio"] or 0) >= 0.9
        and int(payload["open_alerts"] or 0) == 0
    )
    return {"required": True, "ok": payload["ok"], "latest": payload}


if __name__ == "__main__":
    main()
