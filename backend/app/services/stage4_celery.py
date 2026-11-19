from __future__ import annotations

from celery import Celery

from app.core.config import get_settings


settings = get_settings()

celery_app = Celery(
    "map1_stage4",
    broker=settings.stage4_queue_url,
    backend=settings.stage4_result_backend_url or settings.stage4_queue_url,
    include=["app.tasks.stage4"],
)

celery_app.conf.update(
    task_default_queue="stage4",
    task_routes={"stage4.*": {"queue": "stage4"}},
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=settings.stage4_task_soft_time_limit_seconds,
    task_time_limit=settings.stage4_task_time_limit_seconds,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.stage4_provider_max_concurrency,
    result_expires=60 * 60 * 24,
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    "stage4-schedule-layer-b-update": {
        "task": "stage4.schedule_layer_b_update",
        "schedule": 60 * 60 * 24,
    },
    "stage4-monitor-processes": {
        "task": "stage4.monitor_processes",
        "schedule": 15 * 60,
    },
}
