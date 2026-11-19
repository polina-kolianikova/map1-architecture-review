from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.stage4_automation import load_queued_batch_ids, process_batch, refresh_run_rollup


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    with SessionLocal() as db:
        run = _load_run(db, run_id=args.run_id, run_uuid=args.run_uuid)
        reset = _reset_repairable_batches(
            db,
            run_id=int(run["id"]),
            include_failed=args.reset_failed,
            include_stuck=args.reset_stuck,
            error_contains=args.error_contains,
        ) if args.reset_failed or args.reset_stuck else []
        if reset:
            _resolve_reset_alerts(db, run_id=int(run["id"]), error_contains=args.error_contains)
        refresh_run_rollup(db, int(run["id"]))
        db.commit()

    queued_batches = []
    with SessionLocal() as db:
        queued_batches_total = load_queued_batch_ids(db, int(run["id"]))
        queued_batches = queued_batches_total[: args.max_batches] if args.max_batches else queued_batches_total

    enqueued = 0
    cleared_messages = 0
    processed_inline = []
    if args.clear_stage4_queue:
        cleared_messages = _clear_stage4_queue(settings)
    if args.enqueue and queued_batches:
        from app.tasks.stage4 import enqueue_batches

        enqueued = enqueue_batches(queued_batches)
    if args.process_inline and queued_batches:
        processed_inline = [process_batch(batch_id, settings=settings) for batch_id in queued_batches]

    print(
        json.dumps(
            {
                "run_id": int(run["id"]),
                "run_uuid": str(run["run_uuid"]),
                "reset_failed_batches": reset,
                "queued_batches_total": len(queued_batches_total),
                "queued_batches": len(queued_batches),
                "enqueued": enqueued,
                "processed_inline": processed_inline,
                "cleared_stage4_queue_messages": cleared_messages,
                "note": "Stop the worker before clearing or re-enqueueing the Stage 4 queue.",
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resume an existing Stage 4 run without creating a duplicate full run")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id", type=int)
    target.add_argument("--run-uuid")
    parser.add_argument("--reset-failed", action="store_true")
    parser.add_argument("--reset-stuck", action="store_true")
    parser.add_argument("--error-contains")
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--process-inline", action="store_true")
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--clear-stage4-queue", action="store_true")
    args = parser.parse_args()
    if args.error_contains and not (args.reset_failed or args.reset_stuck):
        raise ValueError("--error-contains requires --reset-failed or --reset-stuck")
    if args.clear_stage4_queue and not args.enqueue:
        raise ValueError("--clear-stage4-queue requires --enqueue")
    if args.enqueue and args.process_inline:
        raise ValueError("--enqueue cannot be combined with --process-inline")
    if args.max_batches is not None and args.max_batches <= 0:
        raise ValueError("--max-batches must be greater than zero")
    return args


def _load_run(db, *, run_id: int | None, run_uuid: str | None) -> dict:
    if run_id is not None:
        row = db.execute(
            text("SELECT * FROM stage4_layer_b_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().one_or_none()
    else:
        row = db.execute(
            text("SELECT * FROM stage4_layer_b_runs WHERE run_uuid = CAST(:run_uuid AS uuid)"),
            {"run_uuid": run_uuid},
        ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("Stage 4 run was not found")
    return dict(row)


def _reset_repairable_batches(
    db,
    *,
    run_id: int,
    include_failed: bool,
    include_stuck: bool,
    error_contains: str | None,
) -> list[dict]:
    statuses = []
    if include_failed:
        statuses.append("failed")
    if include_stuck:
        statuses.append("stuck")
    if not statuses:
        return []
    params = {"run_id": run_id, "error_contains": error_contains}
    status_filter = ", ".join(f"'{status}'" for status in statuses)
    error_filter = "AND latest_error ILIKE '%' || :error_contains || '%'" if error_contains else ""
    rows = db.execute(
        text(
            f"""
            WITH failed AS (
                SELECT id
                FROM stage4_layer_b_batches
                WHERE run_id = :run_id
                  AND status IN ({status_filter})
                  {error_filter}
            ), updated AS (
                UPDATE stage4_layer_b_batches batch
                SET status = 'queued',
                    processed_cells = 0,
                    succeeded_cells = 0,
                    failed_cells = 0,
                    skipped_cells = 0,
                    latest_error = NULL,
                    locked_at = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    heartbeat_at = NULL,
                    celery_task_id = NULL,
                    updated_at = now()
                FROM failed
                WHERE batch.id = failed.id
                RETURNING batch.id, batch.task_name, batch.batch_index, batch.status
            )
            SELECT id, task_name, batch_index, status
            FROM updated
            ORDER BY
                CASE task_name
                    WHEN 'ndvi_update' THEN 1
                    WHEN 'biomass_update' THEN 2
                    ELSE 9
                END,
                batch_index
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def _resolve_reset_alerts(db, *, run_id: int, error_contains: str | None) -> None:
    params = {"run_id": run_id, "error_contains": error_contains}
    error_filter = "AND details::text ILIKE '%' || :error_contains || '%'" if error_contains else ""
    db.execute(
        text(
            f"""
            UPDATE stage4_alerts
            SET status = 'resolved',
                resolved_at = now(),
                last_seen_at = now()
            WHERE run_id = :run_id
              AND status <> 'resolved'
              AND alert_type IN ('batch_failed', 'batch_stuck')
              {error_filter}
            """
        ),
        params,
    )


def _clear_stage4_queue(settings) -> int:
    from redis import Redis

    client = Redis.from_url(settings.stage4_queue_url)
    queued_messages = int(client.llen("stage4") or 0)
    if queued_messages:
        client.delete("stage4")
    return queued_messages


if __name__ == "__main__":
    main()
