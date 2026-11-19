from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.api.stage1 import _jsonable, stage1_params
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.forest_scope import component_extent_info, require_component_extent_publication
from app.services.stage3_partitioned_reads import grid_cells_table_sql


router = APIRouter(prefix="/stage4", tags=["stage4"])


@router.get("/status")
def stage4_status(
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    settings = get_settings()
    params = stage1_params(
        region_code=region_code or settings.stage4_region_code,
        grid_version=grid_version or settings.stage4_grid_version,
    )
    with SessionLocal() as db:
        latest_run = _latest_run(db, params)
        return {
            "stage": "stage4",
            "scope": "layer_b_automation",
            "region_code": params["region_code"],
            "grid_version": params["grid_version"],
            "scheduler_enabled": settings.stage4_scheduler_enabled,
            "cadence_days": settings.stage4_update_cadence_days,
            "batch_size": settings.stage4_batch_size,
            "latest_run": _jsonable_or_none(_public_run_payload(latest_run)),
            "open_alerts": _open_alert_count(db, params),
            "heartbeats": [_jsonable(row) for row in _heartbeats(db)],
            "metric_coverage": [
                _jsonable(row)
                for row in _metric_coverage(
                    db,
                    latest_run=latest_run,
                    biomass_formula_enabled=settings.stage4_biomass_formula_enabled,
                )
            ],
            "recent_events": [_jsonable(row) for row in _recent_events(db, params)],
            "area_coefficient": _jsonable(_area_coefficient_status(db, params, latest_run=latest_run)),
        }


@router.get("/runs")
def list_stage4_runs(
    limit: int = Query(20, ge=1, le=100),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    settings = get_settings()
    params = {
        **stage1_params(
            region_code=region_code or settings.stage4_region_code,
            grid_version=grid_version or settings.stage4_grid_version,
        ),
        "limit": limit,
    }
    with SessionLocal() as db:
        rows = db.execute(
            text(
                """
                SELECT
                    run_uuid::text AS run_uuid,
                    region_code,
                    grid_version,
                    trigger_source,
                    status,
                    cadence_days,
                    total_cells,
                    total_batches,
                    queued_batches,
                    processed_cells,
                    succeeded_cells,
                    failed_cells,
                    skipped_cells,
                    (latest_error IS NOT NULL) AS has_error,
                    requested_start::text AS requested_start,
                    requested_end::text AS requested_end,
                    created_at,
                    started_at,
                    finished_at,
                    updated_at
                FROM stage4_layer_b_runs
                WHERE region_code = :region_code
                  AND grid_version = :grid_version
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    return {"rows": [_jsonable(row) for row in rows]}


@router.get("/runs/{run_uuid}")
def get_stage4_run(run_uuid: UUID):
    with SessionLocal() as db:
        run = db.execute(
            text(
                """
                SELECT
                    id,
                    run_uuid::text AS run_uuid,
                    region_code,
                    grid_version,
                    trigger_source,
                    status,
                    cadence_days,
                    total_cells,
                    total_batches,
                    queued_batches,
                    processed_cells,
                    succeeded_cells,
                    failed_cells,
                    skipped_cells,
                    (latest_error IS NOT NULL) AS has_error,
                    requested_start::text AS requested_start,
                    requested_end::text AS requested_end,
                    created_at,
                    started_at,
                    finished_at,
                    updated_at
                FROM stage4_layer_b_runs
                WHERE run_uuid = :run_uuid
                """
            ),
            {"run_uuid": str(run_uuid)},
        ).mappings().one_or_none()
        if run is None:
            return {"run": None, "batches": [], "alerts": [], "events": []}
        batches = db.execute(
            text(
                """
                SELECT
                    id,
                    task_name,
                    batch_index,
                    grid_cell_id_start,
                    grid_cell_id_end,
                    expected_cells,
                    processed_cells,
                    succeeded_cells,
                    failed_cells,
                    skipped_cells,
                    status,
                    (latest_error IS NOT NULL) AS has_error,
                    attempts,
                    started_at,
                    finished_at,
                    heartbeat_at,
                    updated_at
                FROM stage4_layer_b_batches
                WHERE run_id = :run_id
                ORDER BY task_name, batch_index
                LIMIT 500
                """
            ),
            {"run_id": int(run["id"])},
        ).mappings().all()
        alerts = db.execute(
            text(
                """
                SELECT
                    id,
                    severity,
                    alert_type,
                    status,
                    message,
                    (details <> '{}'::jsonb) AS has_details,
                    first_seen_at,
                    last_seen_at,
                    resolved_at
                FROM stage4_alerts
                WHERE run_id = :run_id
                ORDER BY last_seen_at DESC, id DESC
                LIMIT 100
                """
            ),
            {"run_id": int(run["id"])},
        ).mappings().all()
        events = db.execute(
            text(
                """
                SELECT
                    id,
                    severity,
                    event_type,
                    message,
                    created_at
                FROM stage4_layer_b_events
                WHERE run_id = :run_id
                ORDER BY created_at DESC, id DESC
                LIMIT 200
                """
            ),
            {"run_id": int(run["id"])},
        ).mappings().all()
    run_payload = dict(run)
    run_payload.pop("id", None)
    return {
        "run": _jsonable(_public_run_payload(run_payload)),
        "batches": [_jsonable(row) for row in batches],
        "alerts": [_jsonable(row) for row in alerts],
        "events": [_jsonable(row) for row in events],
    }


@router.get("/alerts")
def list_stage4_alerts(
    limit: int = Query(50, ge=1, le=200),
    status: str = Query("open"),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    status = status.strip().lower()
    if status not in {"open", "acknowledged", "resolved", "all"}:
        status = "open"
    settings = get_settings()
    params = {
        **stage1_params(
            region_code=region_code or settings.stage4_region_code,
            grid_version=grid_version or settings.stage4_grid_version,
        ),
        "limit": limit,
        "status": status,
    }
    with SessionLocal() as db:
        rows = db.execute(
            text(
                """
                SELECT
                    alert.id,
                    alert.severity,
                    alert.alert_type,
                    alert.status,
                    alert.message,
                    (alert.details <> '{}'::jsonb) AS has_details,
                    alert.first_seen_at,
                    alert.last_seen_at,
                    alert.resolved_at,
                    run.run_uuid::text AS run_uuid,
                    alert.batch_id
                FROM stage4_alerts alert
                LEFT JOIN stage4_layer_b_runs run ON run.id = alert.run_id
                WHERE (:status = 'all' OR alert.status = :status)
                  AND (
                      alert.run_id IS NULL
                      OR (
                          run.region_code = :region_code
                          AND run.grid_version = :grid_version
                      )
                  )
                ORDER BY alert.last_seen_at DESC, alert.id DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    return {"rows": [_jsonable(row) for row in rows]}


@router.get("/assets/{cell_uuid}/metrics")
def get_stage4_asset_metrics(
    cell_uuid: UUID,
    metric_name: str = Query("all"),
    months: int = Query(12, ge=1, le=36),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    metric_name = metric_name.strip().lower()
    if metric_name not in {"all", "ndvi", "biomass"}:
        metric_name = "all"
    settings = get_settings()
    params = {
        **stage1_params(
            region_code=region_code or settings.stage4_region_code,
            grid_version=grid_version or settings.stage4_grid_version,
        ),
        "cell_uuid": str(cell_uuid),
        "metric_name": metric_name,
        "months": months,
    }
    grid_table = grid_cells_table_sql(params)
    with SessionLocal() as db:
        rows = db.execute(
            text(
                f"""
                WITH target_cell AS (
                    SELECT cell.id
                    FROM {grid_table} cell
                    JOIN regions region ON region.id = cell.region_id
                    WHERE cell.uuid = :cell_uuid
                      AND region.code = :region_code
                      AND cell.grid_version = :grid_version
                    LIMIT 1
                ),
                anchor AS (
                    SELECT COALESCE(max(metric_date), CURRENT_DATE) AS latest_metric_date
                    FROM stage4_layer_b_metrics
                    WHERE grid_cell_id = (SELECT id FROM target_cell)
                      AND (:metric_name = 'all' OR metric_name = :metric_name)
                )
                SELECT
                    metric_name,
                    metric_date::text AS metric_date,
                    value,
                    per_hectare_value,
                    area_coefficient,
                    area_weighted_value,
                    unit,
                    source_status,
                    source_provider,
                    quality_score,
                    created_at,
                    updated_at
                FROM stage4_layer_b_metrics
                WHERE grid_cell_id = (SELECT id FROM target_cell)
                  AND (:metric_name = 'all' OR metric_name = :metric_name)
                  AND metric_date >= (
                        SELECT latest_metric_date
                        FROM anchor
                  ) - (:months * INTERVAL '1 month')
                ORDER BY metric_name, metric_date, id
                LIMIT 1000
                """
            ),
            params,
        ).mappings().all()
    return {
        "cell_uuid": str(cell_uuid),
        "metric_name": metric_name,
        "months": months,
        "rows": [_jsonable(row) for row in rows],
    }


def _latest_run(db, params: dict[str, object]):
    return db.execute(
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
                (latest_error IS NOT NULL) AS has_error,
                requested_start::text AS requested_start,
                requested_end::text AS requested_end,
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
        params,
    ).mappings().one_or_none()


def _jsonable_or_none(row):
    return _jsonable(row) if row is not None else None


def _public_run_payload(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    payload.pop("id", None)
    payload.pop("metadata", None)
    payload.pop("latest_error", None)
    return payload


def _open_alert_count(db, params: dict[str, object]) -> int:
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
            params,
        ).scalar_one()
        or 0
    )


def _heartbeats(db):
    return db.execute(
        text(
            """
            SELECT
                service_name,
                worker_name,
                status,
                (metadata <> '{}'::jsonb) AS has_metadata,
                heartbeat_at
            FROM stage4_process_heartbeats
            ORDER BY heartbeat_at DESC
            LIMIT 20
            """
        )
    ).mappings().all()


def _metric_coverage(db, *, latest_run: Mapping[str, Any] | None, biomass_formula_enabled: bool):
    if latest_run is None:
        return []
    return db.execute(
        text(
            """
            WITH target_run AS (
                SELECT id, requested_end
                FROM stage4_layer_b_runs
                WHERE run_uuid = CAST(:run_uuid AS uuid)
                LIMIT 1
            ),
            task_rollup AS (
                SELECT
                    batch.task_name,
                    COALESCE(sum(batch.processed_cells), 0)::bigint AS processed_cells,
                    COALESCE(sum(batch.failed_cells), 0)::bigint AS failed_cells,
                    max(batch.finished_at) AS latest_finished_at
                FROM stage4_layer_b_batches batch
                JOIN target_run run ON run.id = batch.run_id
                GROUP BY batch.task_name
            )
            SELECT
                CASE
                    WHEN task.task_name = 'ndvi_update' THEN 'ndvi'
                    WHEN task.task_name = 'biomass_update' THEN 'biomass'
                    ELSE task.task_name
                END AS metric_name,
                CASE
                    WHEN task.failed_cells > 0 THEN 'failed'
                    WHEN task.task_name = 'biomass_update'
                         AND NOT CAST(:biomass_formula_enabled AS boolean)
                    THEN 'pending_formula'
                    ELSE 'processed'
                END AS source_status,
                task.processed_cells AS rows,
                task.processed_cells AS cells,
                COALESCE(task.latest_finished_at::date, run.requested_end)::text AS latest_metric_date,
                'batch_rollup' AS coverage_source,
                false AS exact_counts
            FROM task_rollup task
            CROSS JOIN target_run run
            WHERE task.processed_cells > 0
            ORDER BY metric_name, source_status
            """
        ),
        {
            "run_uuid": str(latest_run["run_uuid"]),
            "biomass_formula_enabled": bool(biomass_formula_enabled),
        },
    ).mappings().all()


def _recent_events(db, params: dict[str, object]):
    return db.execute(
        text(
            """
            SELECT
                event.severity,
                event.event_type,
                event.message,
                (event.details <> '{}'::jsonb) AS has_details,
                event.created_at,
                run.run_uuid::text AS run_uuid
            FROM stage4_layer_b_events event
            JOIN stage4_layer_b_runs run ON run.id = event.run_id
            WHERE run.region_code = :region_code
              AND run.grid_version = :grid_version
            ORDER BY event.created_at DESC, event.id DESC
            LIMIT 20
            """
        ),
        params,
    ).mappings().all()


def _area_coefficient_status(
    db,
    params: dict[str, object],
    *,
    latest_run: Mapping[str, Any] | None = None,
):
    if latest_run is not None and latest_run.get("total_cells") is not None:
        total_cells = int(latest_run["total_cells"] or 0)
        return {
            "total_cells": total_cells,
            "populated_cells": total_cells,
            "missing_cells": 0,
            "min_area_coefficient": None,
            "max_area_coefficient": None,
            "status_source": "stage4_run_preflight",
            "exact_extremes": False,
        }
    if latest_run is None:
        region = db.execute(
            text("SELECT id, metadata FROM regions WHERE code = :region_code"), params,
        ).mappings().one_or_none()
        try:
            component_scope = region is not None and component_extent_info(region["metadata"]) is not None
            if component_scope:
                require_component_extent_publication(
                    db, region_id=int(region["id"]), metadata=region["metadata"],
                )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if component_scope:
            total_cells = db.execute(text("""
                SELECT run.inserted_cells
                FROM stage3_grid_generation_runs run
                WHERE run.region_id = :region_id AND run.grid_version = :grid_version
                  AND run.status = 'completed' AND run.inserted_cells IS NOT NULL
                ORDER BY run.finished_at DESC NULLS LAST, run.id DESC
                LIMIT 1
            """), {**params, "region_id": int(region["id"])}).scalar_one_or_none()
            if total_cells is not None:
                return {
                    "total_cells": int(total_cells),
                    "populated_cells": None,
                    "missing_cells": None,
                    "min_area_coefficient": None,
                    "max_area_coefficient": None,
                    "status_source": "stage3_completed_run",
                    "exact_total": True,
                    "exact_counts": False,
                    "exact_extremes": False,
                }
    grid_table = grid_cells_table_sql(params)
    return db.execute(
        text(
            f"""
            SELECT
                count(*)::bigint AS total_cells,
                count(*) FILTER (
                    WHERE cell.area_coefficient IS NOT NULL
                )::bigint AS populated_cells,
                count(*) FILTER (
                    WHERE cell.area_coefficient IS NULL
                )::bigint AS missing_cells,
                min(cell.area_coefficient) AS min_area_coefficient,
                max(cell.area_coefficient) AS max_area_coefficient
            FROM {grid_table} cell
            WHERE cell.region_id = (
                  SELECT id
                  FROM regions
                  WHERE code = :region_code
              )
              AND cell.grid_version = :grid_version
            """
        ),
        params,
    ).mappings().one()
