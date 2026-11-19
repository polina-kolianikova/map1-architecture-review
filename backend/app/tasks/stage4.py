from __future__ import annotations

raise RuntimeError("Private processing producers are not distributed and cannot be started.")


from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import (
    acquire_stage4_schedule_lock,
    create_stage4_run,
    default_update_period,
    detect_stuck_batches,
    due_for_scheduled_run,
    enabled_task_names,
    load_queued_batch_ids,
    poll_batch_statistical_requests,
    process_batch,
    record_heartbeat,
    require_stage4_provider_preflight,
    set_batch_task_id,
)
from app.services.stage4_celery import celery_app
from app.services.stage4_providers import stage4_provider_capabilities


@celery_app.task(name="stage4.schedule_layer_b_update")
def schedule_layer_b_update(region_code: str | None = None, grid_version: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.stage4_scheduler_enabled:
        return {"scheduled": False, "reason": "scheduler_disabled"}

    region_code = region_code or settings.stage4_region_code
    grid_version = grid_version or settings.stage4_grid_version
    requested_start, requested_end = default_update_period()
    with SessionLocal() as db:
        if not acquire_stage4_schedule_lock(db, region_code=region_code, grid_version=grid_version):
            return {
                "scheduled": False,
                "reason": "schedule_lock_busy",
                "region_code": region_code,
                "grid_version": grid_version,
            }
        if not due_for_scheduled_run(
            db,
            region_code=region_code,
            grid_version=grid_version,
            cadence_days=settings.stage4_update_cadence_days,
        ):
            return {
                "scheduled": False,
                "reason": "cadence_window_open",
                "region_code": region_code,
                "grid_version": grid_version,
            }
        provider_preflight = require_stage4_provider_preflight(settings)
        run = create_stage4_run(
            db,
            region_code=region_code,
            grid_version=grid_version,
            trigger_source="celery-beat",
            requested_start=requested_start,
            requested_end=requested_end,
            cadence_days=settings.stage4_update_cadence_days,
            max_cloud_coverage=settings.stage4_max_cloud_coverage,
            min_valid_pixel_ratio=settings.stage4_min_valid_pixel_ratio,
            resolution=settings.stage4_statistics_resolution,
            batch_size=settings.stage4_batch_size,
            task_names=enabled_task_names(settings),
            metadata={
                "scheduled_at": datetime.now(UTC).isoformat(),
                "provider_preflight": provider_preflight,
            },
        )
        batch_ids = load_queued_batch_ids(db, int(run["id"]))
        db.commit()

    queued = enqueue_batches(batch_ids)
    return {
        "scheduled": True,
        "run_uuid": str(run["run_uuid"]),
        "run_id": int(run["id"]),
        "queued_batches": queued,
        "region_code": region_code,
        "grid_version": grid_version,
    }


@celery_app.task(name="stage4.process_batch", bind=True, max_retries=None)
def process_stage4_batch(self, batch_id: int) -> dict[str, Any]:
    settings = get_settings()
    result = process_batch(batch_id)
    if result.get("status") in {"waiting_for_ndvi", "batch_busy", "lease_lost"}:
        raise self.retry(countdown=60, max_retries=None)
    if result.get("status") == "source_unavailable":
        raise self.retry(countdown=settings.stage4_source_unavailable_retry_seconds, max_retries=None)
    return result


@celery_app.task(name="stage4.monitor_processes")
def monitor_processes() -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        record_heartbeat(
            db,
            service_name="stage4-monitor",
            worker_name="celery-beat",
            status="running",
            metadata={"checked_at": datetime.now(UTC).isoformat()},
        )
        stuck = detect_stuck_batches(
            db,
            stuck_after_minutes=settings.stage4_stuck_after_minutes,
        )
        capabilities = stage4_provider_capabilities(settings)
        batch_statistical = (
            poll_batch_statistical_requests(db, settings=settings)
            if capabilities.uses_external_jobs
            else []
        )
        db.commit()
    ready_batch_ids = [
        int(result["batch_id"])
        for result in batch_statistical
        if result.get("batch_status") == "external_ready"
    ]
    queued_imports = enqueue_batches(ready_batch_ids) if ready_batch_ids else 0
    return {
        "stuck_batches": len(stuck),
        "batch_statistical_updates": len(batch_statistical),
        "batch_statistical_imports_queued": queued_imports,
    }


def enqueue_batches(batch_ids: list[int]) -> int:
    queued = 0
    with SessionLocal() as db:
        for batch_id in batch_ids:
            async_result = process_stage4_batch.delay(batch_id)
            set_batch_task_id(db, batch_id, async_result.id)
            queued += 1
        db.commit()
    return queued
