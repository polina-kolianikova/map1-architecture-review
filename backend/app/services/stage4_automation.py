from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from threading import get_ident
from time import sleep
from typing import Any

from sqlalchemy import event, text

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal, engine
from app.services.stage3_partitioned_reads import grid_cells_table_sql
from app.services.stage4_public_sources import (
    PUBLIC_STAC_COG_SOURCE_ALGORITHM,
    load_valid_public_raster_cache,
    public_raster_cache_identity,
    public_raster_cache_key,
    public_raster_cache_metadata,
    public_source_adapter_capabilities,
    public_source_plan,
    read_public_raster_cache_source_metadata,
    write_public_raster_cache,
    write_public_raster_cache_file,
)
from app.services.stage4_providers import get_stage4_ndvi_provider, stage4_provider_capabilities


STAGE4_NDVI_PIPELINE_NAME = "stage4_layer_b_ndvi"
STAGE4_BIOMASS_PIPELINE_NAME = "stage4_layer_b_biomass"
TASK_NDVI_UPDATE = "ndvi_update"
TASK_BIOMASS_UPDATE = "biomass_update"
TASK_NAMES = (TASK_NDVI_UPDATE, TASK_BIOMASS_UPDATE)
TERMINAL_BATCH_STATUSES = {
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
    "skipped",
    "stuck",
}
PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER = (
    "CDSE public statistics adapter is diagnostic-only; use public_raster_zonal for full-scope Stage 4"
)


@dataclass(frozen=True)
class Stage4Scope:
    region_id: int
    region_code: str
    grid_version: str
    total_cells: int
    min_cell_id: int | None
    max_cell_id: int | None
    missing_area_coefficient_cells: int


@dataclass(frozen=True)
class Stage4BatchResult:
    processed_cells: int
    succeeded_cells: int
    failed_cells: int
    skipped_cells: int
    latest_error: str | None = None


@dataclass(frozen=True)
class PublicRasterZonalImportResult:
    batch_result: Stage4BatchResult
    extra_rows: int


@dataclass(frozen=True)
class Stage4BatchingContext:
    strategy: str
    batches_per_task: int
    source_table: str
    spatial_tile_run_id: int | None = None
    spatial_tile_run_uuid: str | None = None
    spatial_tile_count: int = 0
    spatial_tile_cells: int = 0
    spatial_tile_max_cells: int = 0
    blockers: tuple[str, ...] = ()


def area_coefficient_from_area_ha(area_ha: Any) -> Decimal:
    value = Decimal(str(area_ha))
    if value <= 0:
        raise ValueError("area_ha must be greater than zero")
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def batch_ranges(min_cell_id: int | None, max_cell_id: int | None, batch_size: int) -> list[tuple[int, int]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if min_cell_id is None or max_cell_id is None:
        return []
    ranges = []
    start = int(min_cell_id)
    final_id = int(max_cell_id)
    while start <= final_id:
        end = min(start + batch_size - 1, final_id)
        ranges.append((start, end))
        start = end + 1
    return ranges


def batch_range_count(min_cell_id: int | None, max_cell_id: int | None, batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if min_cell_id is None or max_cell_id is None:
        return 0
    span = int(max_cell_id) - int(min_cell_id) + 1
    if span <= 0:
        return 0
    return (span + batch_size - 1) // batch_size


def enabled_task_names(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    names = []
    if settings.stage4_ndvi_update_enabled:
        names.append(TASK_NDVI_UPDATE)
    if settings.stage4_biomass_update_enabled:
        names.append(TASK_BIOMASS_UPDATE)
    return names


def validate_task_names(task_names: list[str] | tuple[str, ...]) -> list[str]:
    normalized = []
    for task_name in task_names:
        clean_name = task_name.strip()
        if clean_name not in TASK_NAMES:
            raise ValueError(f"Unsupported Stage 4 task: {task_name}")
        if clean_name not in normalized:
            normalized.append(clean_name)
    if not normalized:
        raise ValueError("At least one Stage 4 task must be enabled")
    return normalized


def default_update_period(days: int = 45) -> tuple[date, date]:
    end = datetime.now(UTC).date()
    return end - timedelta(days=days), end


def load_scope(db, region_code: str, grid_version: str, grid_table: str = "grid_cells") -> Stage4Scope:
    _ensure_grid_table(grid_table)
    row = db.execute(
        text(
            f"""
            SELECT
                region.id AS region_id,
                region.code AS region_code,
                CAST(:grid_version AS text) AS grid_version,
                count(cell.id)::bigint AS total_cells,
                min(cell.id)::bigint AS min_cell_id,
                max(cell.id)::bigint AS max_cell_id,
                count(cell.id) FILTER (
                    WHERE cell.area_coefficient IS NULL
                )::bigint AS missing_area_coefficient_cells
            FROM regions region
            LEFT JOIN {grid_table} cell
              ON cell.region_id = region.id
             AND cell.grid_version = :grid_version
            WHERE region.code = :region_code
            GROUP BY region.id, region.code
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"region {region_code} does not exist")
    return Stage4Scope(
        region_id=int(row["region_id"]),
        region_code=str(row["region_code"]),
        grid_version=str(row["grid_version"]),
        total_cells=int(row["total_cells"] or 0),
        min_cell_id=int(row["min_cell_id"]) if row["min_cell_id"] is not None else None,
        max_cell_id=int(row["max_cell_id"]) if row["max_cell_id"] is not None else None,
        missing_area_coefficient_cells=int(row["missing_area_coefficient_cells"] or 0),
    )


def limit_scope_to_first_cells(db, scope: Stage4Scope, max_cells: int | None) -> Stage4Scope:
    if max_cells is None:
        return scope
    if max_cells <= 0:
        raise ValueError("max_cells must be greater than zero")
    row = db.execute(
        text(
            """
            WITH selected_cells AS (
                SELECT id, area_coefficient
                FROM grid_cells
                WHERE region_id = :region_id
                  AND grid_version = :grid_version
                ORDER BY id
                LIMIT :max_cells
            )
            SELECT
                count(*)::bigint AS total_cells,
                min(id)::bigint AS min_cell_id,
                max(id)::bigint AS max_cell_id,
                count(*) FILTER (WHERE area_coefficient IS NULL)::bigint AS missing_area_coefficient_cells
            FROM selected_cells
            """
        ),
        {
            "region_id": scope.region_id,
            "grid_version": scope.grid_version,
            "max_cells": max_cells,
        },
    ).mappings().one()
    return Stage4Scope(
        region_id=scope.region_id,
        region_code=scope.region_code,
        grid_version=scope.grid_version,
        total_cells=int(row["total_cells"] or 0),
        min_cell_id=int(row["min_cell_id"]) if row["min_cell_id"] is not None else None,
        max_cell_id=int(row["max_cell_id"]) if row["max_cell_id"] is not None else None,
        missing_area_coefficient_cells=int(row["missing_area_coefficient_cells"] or 0),
    )


def build_stage4_plan(
    db,
    *,
    region_code: str,
    grid_version: str,
    batch_size: int,
    task_names: list[str] | tuple[str, ...],
    max_cells: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    tasks = validate_task_names(task_names)
    scope = limit_scope_to_first_cells(db, load_scope(db, region_code, grid_version), max_cells)
    effective_batch_size = stage4_effective_batch_size(batch_size, settings)
    capabilities = stage4_provider_capabilities(settings)
    source_plan = public_source_plan(settings) if capabilities.adapter == "public_raster_zonal" else None
    batching = stage4_batching_context(
        db,
        scope=scope,
        batch_size=effective_batch_size,
        settings=settings,
    )
    batches_per_task = batching.batches_per_task
    blockers = [*_plan_blockers(scope, settings), *batching.blockers]
    return {
        "stage": "stage4",
        "scope": "layer_b_automation",
        "region_code": scope.region_code,
        "grid_version": scope.grid_version,
        "total_cells": scope.total_cells,
        "missing_area_coefficient_cells": scope.missing_area_coefficient_cells,
        "max_cells": max_cells,
        "batch_size": effective_batch_size,
        "requested_batch_size": batch_size,
        "processing_path": settings.stage4_processing_path,
        "provider_adapter": capabilities.adapter,
        "ndvi_processing_mode": capabilities.ndvi_processing_mode,
        "uses_external_jobs": capabilities.uses_external_jobs,
        "uses_object_storage_exchange": capabilities.uses_object_storage_exchange,
        "diagnostic_only": capabilities.diagnostic_only,
        "supports_full_scope": capabilities.supports_full_scope,
        "public_source": source_plan,
        "batching_strategy": batching.strategy,
        "batching_source_table": batching.source_table,
        "spatial_tile_run_id": batching.spatial_tile_run_id,
        "spatial_tile_run_uuid": batching.spatial_tile_run_uuid,
        "spatial_tile_count": batching.spatial_tile_count,
        "spatial_tile_cells": batching.spatial_tile_cells,
        "spatial_tile_max_cells": batching.spatial_tile_max_cells,
        "batch_statistical_max_features": settings.stage4_batch_statistical_max_features,
        "external_ndvi_batches": batches_per_task if capabilities.uses_external_jobs else 0,
        "task_names": tasks,
        "batches_per_task": batches_per_task,
        "total_batches": batches_per_task * len(tasks),
        "source_tables": {
            "grid": "grid_cells",
            "grid_tiles": batching.source_table if batching.strategy == "stage3_spatial_tiles" else None,
            "history": "stage4_layer_b_metrics",
            "artifacts": "stage4_satellite_artifacts",
        },
        "blockers": blockers,
    }


def create_stage4_run(
    db,
    *,
    region_code: str,
    grid_version: str,
    trigger_source: str,
    requested_start: date,
    requested_end: date,
    cadence_days: int,
    max_cloud_coverage: int,
    min_valid_pixel_ratio: float,
    resolution: float,
    batch_size: int,
    task_names: list[str] | tuple[str, ...],
    max_cells: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = validate_task_names(task_names)
    settings = get_settings()
    scope = limit_scope_to_first_cells(db, load_scope(db, region_code, grid_version), max_cells)
    effective_batch_size = stage4_effective_batch_size(batch_size, settings)
    batching = stage4_batching_context(
        db,
        scope=scope,
        batch_size=effective_batch_size,
        settings=settings,
    )
    blockers = [*_plan_blockers(scope, settings), *batching.blockers]
    if blockers:
        raise RuntimeError("; ".join(blockers))
    run = db.execute(
        text(
            """
            INSERT INTO stage4_layer_b_runs (
                region_id,
                region_code,
                grid_version,
                trigger_source,
                cadence_days,
                requested_start,
                requested_end,
                max_cloud_coverage,
                min_valid_pixel_ratio,
                resolution,
                batch_size,
                total_cells,
                status,
                metadata
            )
            VALUES (
                :region_id,
                :region_code,
                :grid_version,
                :trigger_source,
                :cadence_days,
                :requested_start,
                :requested_end,
                :max_cloud_coverage,
                :min_valid_pixel_ratio,
                :resolution,
                :batch_size,
                :total_cells,
                'planned',
                CAST(:metadata AS jsonb)
            )
            RETURNING *
            """
        ),
        {
            "region_id": scope.region_id,
            "region_code": scope.region_code,
            "grid_version": scope.grid_version,
            "trigger_source": trigger_source,
            "cadence_days": cadence_days,
            "requested_start": requested_start,
            "requested_end": requested_end,
            "max_cloud_coverage": max_cloud_coverage,
            "min_valid_pixel_ratio": min_valid_pixel_ratio,
            "resolution": resolution,
            "batch_size": effective_batch_size,
            "total_cells": scope.total_cells,
            "metadata": _json(
                {
                    **(metadata or {}),
                    "requested_batch_size": batch_size,
                    "max_cells": max_cells,
                    "processing_path": settings.stage4_processing_path,
                    "provider_adapter": stage4_provider_capabilities(settings).adapter,
                    "public_source": public_source_plan(settings)
                    if stage4_provider_capabilities(settings).adapter == "public_raster_zonal"
                    else None,
                    "batching_strategy": batching.strategy,
                    "spatial_tile_run_id": batching.spatial_tile_run_id,
                    "spatial_tile_run_uuid": batching.spatial_tile_run_uuid,
                }
            ),
        },
    ).mappings().one()
    run_id = int(run["id"])
    total_batches = create_stage4_batches(
        db,
        run_id=run_id,
        scope=scope,
        batch_size=effective_batch_size,
        task_names=tasks,
        settings=settings,
    )
    db.execute(
        text(
            """
            UPDATE stage4_layer_b_runs
            SET status = 'queued',
                total_batches = :total_batches,
                queued_batches = :total_batches,
                updated_at = now()
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id, "total_batches": total_batches},
    )
    return dict(
        db.execute(
            text("SELECT * FROM stage4_layer_b_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().one()
    )


def stage4_effective_batch_size(batch_size: int, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    capabilities = stage4_provider_capabilities(settings)
    if capabilities.adapter == "industrial_batch_statistical":
        return min(batch_size, settings.stage4_batch_statistical_max_features)
    if capabilities.adapter == "cdse_public_statistics":
        return min(batch_size, settings.stage4_public_statistics_max_cells_per_batch)
    if capabilities.adapter == "public_raster_zonal":
        return min(batch_size, settings.stage4_public_raster_zonal_max_cells_per_batch)
    return batch_size


def stage4_batching_context(
    db,
    *,
    scope: Stage4Scope,
    batch_size: int,
    settings: Settings | None = None,
) -> Stage4BatchingContext:
    settings = settings or get_settings()
    capabilities = stage4_provider_capabilities(settings)
    if capabilities.batching_strategy != "stage3_spatial_tiles":
        return Stage4BatchingContext(
            strategy=capabilities.batching_strategy,
            batches_per_task=batch_range_count(scope.min_cell_id, scope.max_cell_id, batch_size),
            source_table="grid_cells",
        )

    summary = _stage3_spatial_tile_summary(
        db,
        scope,
        prefer_stage3_counts=scope.total_cells > settings.stage4_public_statistics_hard_ceiling_cells,
    )
    blockers = []
    if summary is None:
        blockers.append("Public raster-zonal Stage 4 requires a completed Stage 3 spatial tile queue")
        return Stage4BatchingContext(
            strategy=capabilities.batching_strategy,
            batches_per_task=0,
            source_table="stage3_grid_generation_tiles",
            blockers=tuple(blockers),
        )
    if int(summary["tile_count"] or 0) <= 0:
        blockers.append("Public raster-zonal Stage 4 found no completed spatial tiles with grid cells")
    if int(summary["tile_cells"] or 0) < scope.total_cells:
        blockers.append("Public raster-zonal spatial tiles do not cover the selected Stage 4 scope")
    max_tile_cells = int(summary.get("max_tile_cells") or 0)
    if max_tile_cells > settings.stage4_public_raster_zonal_max_cells_per_batch:
        blockers.append("Public raster-zonal spatial tile exceeds configured cells-per-batch limit")

    return Stage4BatchingContext(
        strategy=capabilities.batching_strategy,
        batches_per_task=int(summary["tile_count"] or 0),
        source_table="stage3_grid_generation_tiles",
        spatial_tile_run_id=int(summary["run_id"]),
        spatial_tile_run_uuid=str(summary["run_uuid"]),
        spatial_tile_count=int(summary["tile_count"] or 0),
        spatial_tile_cells=int(summary["tile_cells"] or 0),
        spatial_tile_max_cells=max_tile_cells,
        blockers=tuple(blockers),
    )


def create_stage4_batches(
    db,
    *,
    run_id: int,
    scope: Stage4Scope,
    batch_size: int,
    task_names: list[str],
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    capabilities = stage4_provider_capabilities(settings)
    if capabilities.batching_strategy == "stage3_spatial_tiles":
        return _create_stage4_spatial_tile_batches(
            db,
            run_id=run_id,
            scope=scope,
            task_names=task_names,
            prefer_stage3_counts=scope.total_cells > settings.stage4_public_statistics_hard_ceiling_cells,
        )

    ranges = batch_ranges(scope.min_cell_id, scope.max_cell_id, batch_size)
    inserted = 0
    for task_name in task_names:
        for index, (start_id, end_id) in enumerate(ranges, start=1):
            expected_cells = db.execute(
                text(
                    """
                    SELECT count(*)::integer
                    FROM grid_cells
                    WHERE region_id = :region_id
                      AND grid_version = :grid_version
                      AND id BETWEEN :start_id AND :end_id
                    """
                ),
                {
                    "region_id": scope.region_id,
                    "grid_version": scope.grid_version,
                    "start_id": start_id,
                    "end_id": end_id,
                },
            ).scalar_one()
            db.execute(
                text(
                    """
                    INSERT INTO stage4_layer_b_batches (
                        run_id,
                        task_name,
                        batch_index,
                        grid_cell_id_start,
                        grid_cell_id_end,
                        expected_cells,
                        metadata
                    )
                    VALUES (
                        :run_id,
                        :task_name,
                        :batch_index,
                        :grid_cell_id_start,
                        :grid_cell_id_end,
                        :expected_cells,
                        CAST(:metadata AS jsonb)
                    )
                    ON CONFLICT (run_id, task_name, batch_index) DO NOTHING
                    """
                ),
                {
                    "run_id": run_id,
                    "task_name": task_name,
                    "batch_index": index,
                    "grid_cell_id_start": start_id,
                    "grid_cell_id_end": end_id,
                    "expected_cells": int(expected_cells),
                    "metadata": _json({"region_code": scope.region_code, "grid_version": scope.grid_version}),
                },
            )
            inserted += 1
    return inserted


def _stage3_spatial_tile_summary(
    db,
    scope: Stage4Scope,
    *,
    prefer_stage3_counts: bool = False,
) -> dict[str, Any] | None:
    if not _table_exists(db, "stage3_grid_generation_runs") or not _table_exists(db, "stage3_grid_generation_tiles"):
        return None
    if prefer_stage3_counts:
        row = db.execute(
            text(
                """
                WITH latest_run AS (
                    SELECT run.*
                    FROM stage3_grid_generation_runs run
                    WHERE run.region_id = :region_id
                      AND run.grid_version = :grid_version
                      AND run.status = 'completed'
                    ORDER BY run.finished_at DESC NULLS LAST, run.started_at DESC, run.id DESC
                    LIMIT 1
                )
                SELECT
                    run.id AS run_id,
                    run.run_id::text AS run_uuid,
                    count(tile.id)::integer AS tile_count,
                    COALESCE(sum(tile.inserted_cells), 0)::bigint AS tile_cells,
                    COALESCE(max(tile.inserted_cells), 0)::integer AS max_tile_cells
                FROM latest_run run
                JOIN stage3_grid_generation_tiles tile ON tile.run_id = run.id
                WHERE tile.status = 'completed'
                  AND tile.inserted_cells > 0
                GROUP BY run.id, run.run_id
                """
            ),
            {
                "region_id": scope.region_id,
                "grid_version": scope.grid_version,
            },
        ).mappings().one_or_none()
        return dict(row) if row is not None else None
    row = db.execute(
        text(
            """
            WITH latest_run AS (
                SELECT run.*
                FROM stage3_grid_generation_runs run
                WHERE run.region_id = :region_id
                  AND run.grid_version = :grid_version
                  AND run.status = 'completed'
                ORDER BY run.finished_at DESC NULLS LAST, run.started_at DESC, run.id DESC
                LIMIT 1
            ),
            tile_scope AS (
                SELECT
                    tile.id,
                    run.id AS run_id,
                    run.run_id AS run_uuid,
                    run.projected_srid,
                    ST_MakeEnvelope(tile.min_x, tile.min_y, tile.max_x, tile.max_y, run.projected_srid)
                        AS geom_projected,
                    ST_Transform(
                        ST_MakeEnvelope(tile.min_x, tile.min_y, tile.max_x, tile.max_y, run.projected_srid),
                        4326
                    ) AS geom_4326
                FROM latest_run run
                JOIN stage3_grid_generation_tiles tile ON tile.run_id = run.id
                WHERE tile.status = 'completed'
            ),
            assigned_cells AS (
                SELECT
                    tile_scope.run_id,
                    tile_scope.run_uuid,
                    tile_scope.id AS tile_id,
                    cell.id AS cell_id,
                    row_number() OVER (PARTITION BY cell.id ORDER BY tile_scope.id) AS tile_rank
                FROM tile_scope
                JOIN grid_cells cell
                  ON cell.region_id = :region_id
                 AND cell.grid_version = :grid_version
                 AND cell.id BETWEEN :min_cell_id AND :max_cell_id
                 AND cell.geom && tile_scope.geom_4326
                 AND ST_Covers(
                        tile_scope.geom_projected,
                        ST_PointOnSurface(ST_Transform(cell.geom, tile_scope.projected_srid))
                     )
            ),
            tile_cells AS (
                SELECT
                    run_id,
                    run_uuid,
                    tile_id,
                    count(cell_id)::bigint AS cell_count
                FROM assigned_cells
                WHERE tile_rank = 1
                GROUP BY run_id, run_uuid, tile_id
            )
            SELECT
                run_id,
                run_uuid::text AS run_uuid,
                count(*)::integer AS tile_count,
                COALESCE(sum(cell_count), 0)::bigint AS tile_cells,
                COALESCE(max(cell_count), 0)::integer AS max_tile_cells
            FROM tile_cells
            WHERE cell_count > 0
            GROUP BY run_id, run_uuid
            """
        ),
        {
            "region_id": scope.region_id,
            "grid_version": scope.grid_version,
            "min_cell_id": scope.min_cell_id or 0,
            "max_cell_id": scope.max_cell_id or 0,
        },
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _create_stage4_spatial_tile_batches(
    db,
    *,
    run_id: int,
    scope: Stage4Scope,
    task_names: list[str],
    prefer_stage3_counts: bool = False,
) -> int:
    if prefer_stage3_counts:
        rows = _stage3_spatial_tile_rows_from_counts(db, scope)
        return _insert_stage4_spatial_tile_batches(db, run_id=run_id, scope=scope, task_names=task_names, rows=rows)
    rows = db.execute(
        text(
            """
            WITH latest_run AS (
                SELECT run.*
                FROM stage3_grid_generation_runs run
                WHERE run.region_id = :region_id
                  AND run.grid_version = :grid_version
                  AND run.status = 'completed'
                ORDER BY run.finished_at DESC NULLS LAST, run.started_at DESC, run.id DESC
                LIMIT 1
            ),
            tile_scope AS (
                SELECT
                    tile.id AS stage3_tile_id,
                    tile.tile_key,
                    tile.min_x,
                    tile.min_y,
                    tile.max_x,
                    tile.max_y,
                    run.id AS stage3_run_id,
                    run.run_id::text AS stage3_run_uuid,
                    run.projected_srid,
                    run.grid_size_m,
                    run.tile_size_m,
                    ST_MakeEnvelope(tile.min_x, tile.min_y, tile.max_x, tile.max_y, run.projected_srid)
                        AS geom_projected,
                    ST_Transform(
                        ST_MakeEnvelope(tile.min_x, tile.min_y, tile.max_x, tile.max_y, run.projected_srid),
                        4326
                    ) AS geom_4326
                FROM latest_run run
                JOIN stage3_grid_generation_tiles tile ON tile.run_id = run.id
                WHERE tile.status = 'completed'
            ),
            assigned_cells AS (
                SELECT
                    tile_scope.stage3_tile_id,
                    tile_scope.tile_key,
                    tile_scope.min_x,
                    tile_scope.min_y,
                    tile_scope.max_x,
                    tile_scope.max_y,
                    tile_scope.stage3_run_id,
                    tile_scope.stage3_run_uuid,
                    tile_scope.projected_srid,
                    tile_scope.grid_size_m,
                    tile_scope.tile_size_m,
                    cell.id AS cell_id,
                    row_number() OVER (PARTITION BY cell.id ORDER BY tile_scope.stage3_tile_id) AS tile_rank
                FROM tile_scope
                JOIN grid_cells cell
                  ON cell.region_id = :region_id
                 AND cell.grid_version = :grid_version
                 AND cell.id BETWEEN :min_cell_id AND :max_cell_id
                 AND cell.geom && tile_scope.geom_4326
                 AND ST_Covers(
                        tile_scope.geom_projected,
                        ST_PointOnSurface(ST_Transform(cell.geom, tile_scope.projected_srid))
                     )
            )
            SELECT
                tile_scope.stage3_tile_id,
                tile_scope.tile_key,
                tile_scope.min_x,
                tile_scope.min_y,
                tile_scope.max_x,
                tile_scope.max_y,
                tile_scope.stage3_run_id,
                tile_scope.stage3_run_uuid,
                tile_scope.projected_srid,
                tile_scope.grid_size_m,
                tile_scope.tile_size_m,
                count(cell.id)::integer AS expected_cells,
                min(cell.id)::bigint AS grid_cell_id_start,
                max(cell.id)::bigint AS grid_cell_id_end
            FROM assigned_cells tile_scope
            JOIN grid_cells cell ON cell.id = tile_scope.cell_id
            WHERE tile_scope.tile_rank = 1
            GROUP BY
                tile_scope.stage3_tile_id,
                tile_scope.tile_key,
                tile_scope.min_x,
                tile_scope.min_y,
                tile_scope.max_x,
                tile_scope.max_y,
                tile_scope.stage3_run_id,
                tile_scope.stage3_run_uuid,
                tile_scope.projected_srid,
                tile_scope.grid_size_m,
                tile_scope.tile_size_m
            HAVING count(cell.id) > 0
            ORDER BY tile_scope.stage3_tile_id
            """
        ),
        {
            "region_id": scope.region_id,
            "grid_version": scope.grid_version,
            "min_cell_id": scope.min_cell_id or 0,
            "max_cell_id": scope.max_cell_id or 0,
        },
    ).mappings().all()
    return _insert_stage4_spatial_tile_batches(db, run_id=run_id, scope=scope, task_names=task_names, rows=rows)


def _stage3_spatial_tile_rows_from_counts(db, scope: Stage4Scope) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            WITH latest_run AS (
                SELECT run.*
                FROM stage3_grid_generation_runs run
                WHERE run.region_id = :region_id
                  AND run.grid_version = :grid_version
                  AND run.status = 'completed'
                ORDER BY run.finished_at DESC NULLS LAST, run.started_at DESC, run.id DESC
                LIMIT 1
            )
            SELECT
                tile.id AS stage3_tile_id,
                tile.tile_key,
                tile.min_x,
                tile.min_y,
                tile.max_x,
                tile.max_y,
                run.id AS stage3_run_id,
                run.run_id::text AS stage3_run_uuid,
                run.projected_srid,
                run.grid_size_m,
                run.tile_size_m,
                tile.inserted_cells::integer AS expected_cells,
                CAST(:min_cell_id AS bigint) AS grid_cell_id_start,
                CAST(:max_cell_id AS bigint) AS grid_cell_id_end
            FROM latest_run run
            JOIN stage3_grid_generation_tiles tile ON tile.run_id = run.id
            WHERE tile.status = 'completed'
              AND tile.inserted_cells > 0
            ORDER BY tile.id
            """
        ),
        {
            "region_id": scope.region_id,
            "grid_version": scope.grid_version,
            "min_cell_id": scope.min_cell_id or 0,
            "max_cell_id": scope.max_cell_id or 0,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _insert_stage4_spatial_tile_batches(
    db,
    *,
    run_id: int,
    scope: Stage4Scope,
    task_names: list[str],
    rows,
) -> int:
    inserted = 0
    for task_name in task_names:
        for index, row in enumerate(rows, start=1):
            db.execute(
                text(
                    """
                    INSERT INTO stage4_layer_b_batches (
                        run_id,
                        task_name,
                        batch_index,
                        grid_cell_id_start,
                        grid_cell_id_end,
                        expected_cells,
                        metadata
                    )
                    VALUES (
                        :run_id,
                        :task_name,
                        :batch_index,
                        :grid_cell_id_start,
                        :grid_cell_id_end,
                        :expected_cells,
                        CAST(:metadata AS jsonb)
                    )
                    ON CONFLICT (run_id, task_name, batch_index) DO NOTHING
                    """
                ),
                {
                    "run_id": run_id,
                    "task_name": task_name,
                    "batch_index": index,
                    "grid_cell_id_start": int(row["grid_cell_id_start"]),
                    "grid_cell_id_end": int(row["grid_cell_id_end"]),
                    "expected_cells": int(row["expected_cells"]),
                    "metadata": _json(
                        {
                            "region_code": scope.region_code,
                            "grid_version": scope.grid_version,
                            "batching_strategy": "stage3_spatial_tiles",
                            "stage3_grid_run_id": int(row["stage3_run_id"]),
                            "stage3_grid_run_uuid": str(row["stage3_run_uuid"]),
                            "stage3_grid_tile_id": int(row["stage3_tile_id"]),
                            "stage3_grid_tile_key": str(row["tile_key"]),
                            "projected_srid": int(row["projected_srid"]),
                            "grid_size_m": int(row["grid_size_m"]),
                            "tile_size_m": int(row["tile_size_m"]),
                            "bbox_projected": [
                                float(row["min_x"]),
                                float(row["min_y"]),
                                float(row["max_x"]),
                                float(row["max_y"]),
                            ],
                        }
                    ),
                },
            )
            inserted += 1
    return inserted


def due_for_scheduled_run(
    db,
    *,
    region_code: str,
    grid_version: str,
    cadence_days: int,
) -> bool:
    latest = db.execute(
        text(
            """
            SELECT COALESCE(finished_at, updated_at, created_at) AS last_run_at
            FROM stage4_layer_b_runs
            WHERE region_code = :region_code
              AND grid_version = :grid_version
              AND status IN (
                  'queued',
                  'running',
                  'external_wait',
                  'external_ready',
                  'completed',
                  'completed_with_errors'
              )
            ORDER BY COALESCE(finished_at, updated_at, created_at) DESC, id DESC
            LIMIT 1
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).scalar_one_or_none()
    if latest is None:
        return True
    return latest <= datetime.now(UTC) - timedelta(days=cadence_days)


def acquire_stage4_schedule_lock(db, *, region_code: str, grid_version: str) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT pg_try_advisory_xact_lock(
                    hashtext('stage4-layer-b'),
                    hashtext(:region_code || ':' || :grid_version)
                )
                """
            ),
            {"region_code": region_code, "grid_version": grid_version},
        ).scalar_one()
    )


def load_queued_batch_ids(db, run_id: int) -> list[int]:
    settings = get_settings()
    rows = db.execute(
        text(
            """
            WITH source_tagged AS (
                SELECT
                    id,
                    task_name,
                    batch_index,
                    status,
                    CASE task_name
                        WHEN 'ndvi_update' THEN 1
                        WHEN 'biomass_update' THEN 2
                        ELSE 9
                    END AS task_order,
                    CASE
                        WHEN metadata->>'batching_strategy' = 'stage3_spatial_tiles'
                        THEN floor(((metadata->'bbox_projected'->>0)::numeric) / :source_tile_size)
                        ELSE 0
                    END AS source_tile_x,
                    CASE
                        WHEN metadata->>'batching_strategy' = 'stage3_spatial_tiles'
                        THEN floor(((metadata->'bbox_projected'->>1)::numeric) / :source_tile_size)
                        ELSE 0
                    END AS source_tile_y
                FROM stage4_layer_b_batches
                WHERE run_id = :run_id
            ),
            source_group_state AS (
                SELECT
                    task_name,
                    source_tile_x,
                    source_tile_y,
                    bool_or(status IN ('completed', 'skipped')) AS source_cache_ready
                FROM source_tagged
                GROUP BY task_name, source_tile_x, source_tile_y
            ),
            queued AS (
                SELECT
                    source_tagged.id,
                    source_tagged.task_name,
                    source_tagged.batch_index,
                    source_tagged.task_order,
                    source_tagged.source_tile_x,
                    source_tagged.source_tile_y,
                    source_group_state.source_cache_ready
                FROM source_tagged
                JOIN source_group_state
                  ON source_group_state.task_name = source_tagged.task_name
                 AND source_group_state.source_tile_x = source_tagged.source_tile_x
                 AND source_group_state.source_tile_y = source_tagged.source_tile_y
                WHERE source_tagged.status = 'queued'
            ),
            interleaved AS (
                SELECT
                    id,
                    task_order,
                    source_tile_x,
                    source_tile_y,
                    batch_index,
                    source_cache_ready,
                    row_number() OVER (
                        PARTITION BY task_name, source_tile_x, source_tile_y
                        ORDER BY batch_index
                    ) AS source_tile_batch_rank
                FROM queued
            )
            SELECT id
            FROM interleaved
            ORDER BY
                task_order,
                CASE WHEN source_cache_ready THEN 0 ELSE 1 END,
                CASE WHEN source_cache_ready THEN source_tile_x ELSE source_tile_batch_rank END,
                CASE WHEN source_cache_ready THEN source_tile_y ELSE source_tile_x END,
                CASE WHEN source_cache_ready THEN batch_index ELSE source_tile_y END,
                batch_index
            """
        ),
        {
            "run_id": run_id,
            "source_tile_size": int(settings.stage4_public_raster_zonal_source_tile_size_m),
        },
    ).mappings().all()
    return [int(row["id"]) for row in rows]


def active_stage4_run(
    db,
    *,
    region_code: str,
    grid_version: str,
) -> dict[str, Any] | None:
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
                created_at,
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


def check_stage4_provider_preflight(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    provider = get_stage4_ndvi_provider(settings)
    return provider.check_permissions(settings)


def require_stage4_provider_preflight(settings: Settings | None = None) -> dict[str, Any]:
    report = check_stage4_provider_preflight(settings)
    if report.get("blocker"):
        raise RuntimeError(f"Stage 4 provider preflight failed: {report['blocker']}")
    if report.get("checked") and not report.get("ok"):
        raise RuntimeError("Stage 4 provider preflight failed")
    return report


def set_batch_task_id(db, batch_id: int, celery_task_id: str) -> None:
    db.execute(
        text(
            """
            UPDATE stage4_layer_b_batches
            SET celery_task_id = :celery_task_id,
                updated_at = now()
            WHERE id = :batch_id
            """
        ),
        {"batch_id": batch_id, "celery_task_id": celery_task_id},
    )


class Stage4BatchLeaseLost(Exception):
    """Stop an old delivery without changing its successor's batch state."""


@dataclass
class _Stage4BatchLease:
    batch_id: int
    connection: Any
    driver_connection: Any

    def assert_owned_connection(self, *_args, **_kwargs) -> None:
        connection = self.connection
        if (
            connection.closed
            or connection.invalidated
            or connection.connection.dbapi_connection is not self.driver_connection
            or getattr(self.driver_connection, "closed", False)
        ):
            raise Stage4BatchLeaseLost(f"Stage 4 batch {self.batch_id} lost its lock connection")


_stage4_batch_lease: ContextVar[_Stage4BatchLease | None] = ContextVar("stage4_batch_lease", default=None)
_STAGE4_BATCH_LOCK_SQL = "hashtextextended('map1:stage4:batch:' || CAST(:batch_id AS text), 0)"


@contextmanager
def _stage4_batch_execution_lock(batch_id: int):
    # Session locks survive commits. Keep this exact physical connection checked
    # out until processing ends, and never let a disconnected worker reconnect.
    with engine.connect() as connection:
        try:
            acquired = bool(connection.execute(
                text(f"SELECT pg_try_advisory_lock({_STAGE4_BATCH_LOCK_SQL})"),
                {"batch_id": int(batch_id)},
            ).scalar_one())
            connection.commit()
        except BaseException:
            connection.invalidate()
            raise
        if not acquired:
            yield None
            return
        lease = _Stage4BatchLease(int(batch_id), connection, connection.connection.dbapi_connection)
        guards = ("begin", "before_execute", "before_cursor_execute", "commit")
        for name in guards:
            event.listen(connection, name, lease.assert_owned_connection)
        token = _stage4_batch_lease.set(lease)
        try:
            yield lease
        finally:
            _stage4_batch_lease.reset(token)
            try:
                lease.assert_owned_connection()
                connection.rollback()
                released = connection.execute(
                    text(f"SELECT pg_advisory_unlock({_STAGE4_BATCH_LOCK_SQL})"),
                    {"batch_id": int(batch_id)},
                ).scalar_one()
                connection.commit()
                if not released:
                    connection.invalidate()
            except Exception:
                # A failed unlock must never return a locked backend to the pool.
                connection.invalidate()
            finally:
                for name in guards:
                    event.remove(connection, name, lease.assert_owned_connection)


@contextmanager
def _stage4_batch_session():
    lease = _stage4_batch_lease.get()
    if lease is None:
        with SessionLocal() as db:
            yield db
        return
    lease.assert_owned_connection()
    # Binding a Session to a Connection preserves that connection across commit
    # and Session.close; it cannot check out a different backend for later writes.
    with SessionLocal(bind=lease.connection) as db:
        yield db


def _with_stage4_batch_lease(processor):
    @wraps(processor)
    def guarded(batch_id: int, settings: Settings | None = None) -> dict[str, Any]:
        try:
            with _stage4_batch_execution_lock(batch_id) as lease:
                if lease is None:
                    return {"batch_id": batch_id, "status": "batch_busy"}
                try:
                    return processor(batch_id, settings)
                except Exception:
                    # The first query/commit after a server disconnect raises a
                    # DBAPI error. Convert it before any failure handler can write
                    # on a replacement connection, including during claim/finish.
                    lease.assert_owned_connection()
                    raise
        except Stage4BatchLeaseLost:
            return {"batch_id": batch_id, "status": "lease_lost"}
    return guarded


@_with_stage4_batch_lease
def process_batch(batch_id: int, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    with _stage4_batch_session() as db:
        if _source_unavailable_circuit_open(db, batch_id, settings):
            return {
                "batch_id": batch_id,
                "status": "source_unavailable",
                "latest_error": "Stage 4 source is temporarily unavailable",
            }
        if _batch_waits_for_ndvi(db, batch_id):
            return {"batch_id": batch_id, "status": "waiting_for_ndvi"}
        batch = _claim_batch(db, batch_id)
        if batch is None:
            return {"batch_id": batch_id, "status": "not_found_or_terminal"}
        run = _load_run(db, int(batch["run_id"]))
        db.commit()

    result = Stage4BatchResult(0, 0, 0, 0)
    try:
        validate_public_statistics_batch_limits(run, batch, settings)
        validate_public_raster_zonal_batch_limits(run, batch, settings)
        if batch["task_name"] == TASK_NDVI_UPDATE:
            provider = get_stage4_ndvi_provider(settings)
            if provider.capabilities.uses_external_jobs:
                return provider.process_batch(run, batch, settings)
            result = provider.process_batch(run, batch, settings)
        elif batch["task_name"] == TASK_BIOMASS_UPDATE:
            result = process_biomass_batch(run, batch, settings)
        else:
            raise RuntimeError(f"Unsupported Stage 4 task: {batch['task_name']}")
    except Stage4BatchLeaseLost:
        raise
    except Exception as exc:
        lease = _stage4_batch_lease.get()
        if lease is not None:
            lease.assert_owned_connection()
        if _is_stage4_source_unavailable(exc):
            latest_error = _short_error(exc)
            with _stage4_batch_session() as db:
                _defer_batch_for_source_unavailable(
                    db,
                    run=run,
                    batch=batch,
                    latest_error=latest_error,
                )
                db.commit()
            return {
                "batch_id": batch_id,
                "status": "source_unavailable",
                "latest_error": latest_error,
            }
        result = Stage4BatchResult(
            processed_cells=0,
            succeeded_cells=0,
            failed_cells=int(batch["expected_cells"] or 0),
            skipped_cells=0,
            latest_error=_short_error(exc),
        )
        with _stage4_batch_session() as db:
            _finish_batch(db, batch_id, "failed", result)
            record_event(
                db,
                run_id=int(run["id"]),
                batch_id=batch_id,
                severity="critical",
                event_type="batch_failed",
                message=f"Stage 4 batch {batch_id} failed",
                details={"error": result.latest_error},
            )
            upsert_alert(
                db,
                run_id=int(run["id"]),
                batch_id=batch_id,
                severity="critical",
                alert_type="batch_failed",
                message=f"Stage 4 batch {batch_id} failed",
                details={"error": result.latest_error},
            )
            refresh_run_rollup(db, int(run["id"]))
            db.commit()
        raise

    status = "completed"
    if result.failed_cells and result.succeeded_cells:
        status = "completed_with_errors"
    elif result.failed_cells and not result.succeeded_cells:
        status = "failed"
    elif result.skipped_cells and not result.succeeded_cells:
        status = "skipped"
    with _stage4_batch_session() as db:
        _finish_batch(db, batch_id, status, result)
        if status == "completed":
            _resolve_source_unavailable_alerts(db, run_id=int(run["id"]))
        if status in {"failed", "completed_with_errors"}:
            record_event(
                db,
                run_id=int(run["id"]),
                batch_id=batch_id,
                severity="critical" if status == "failed" else "warning",
                event_type=status,
                message=f"Stage 4 batch {batch_id} finished with errors",
                details={
                    "failed_cells": result.failed_cells,
                    "latest_error": result.latest_error,
                },
            )
            upsert_alert(
                db,
                run_id=int(run["id"]),
                batch_id=batch_id,
                severity="critical" if status == "failed" else "warning",
                alert_type=status,
                message=f"Stage 4 batch {batch_id} finished with errors",
                details={
                    "failed_cells": result.failed_cells,
                    "latest_error": result.latest_error,
                },
            )
        refresh_run_rollup(db, int(run["id"]))
        db.commit()
    return {"batch_id": batch_id, "status": status, **result.__dict__}


def _is_stage4_source_unavailable(exc: Exception) -> bool:
    from app.services.stage4_public_cog import PublicStacAssetReadError

    if isinstance(exc, PublicStacAssetReadError):
        return True
    if "Public STAC COG scenes were found but none could be read" in str(exc):
        return True
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code not in {401, 403, 429, 500, 502, 503, 504}:
        return False
    url = str(getattr(response, "url", "") or "")
    return (
        "dataspace.copernicus.eu" in url
        or "sentinel-hub.com" in url
        or "earth-search.aws.element84.com" in url
        or "sentinel-cogs.s3" in url
        or "e84-earth-search-sentinel-data.s3" in url
    )


def _source_unavailable_circuit_open(db, batch_id: int, settings: Settings) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT alert.id
                FROM stage4_layer_b_batches batch
                JOIN stage4_alerts alert
                  ON alert.run_id = batch.run_id
                WHERE batch.id = :batch_id
                  AND alert.alert_type = 'source_unavailable'
                  AND alert.status <> 'resolved'
                  AND alert.last_seen_at > now() - (CAST(:cooldown_minutes AS integer) * INTERVAL '1 minute')
                ORDER BY alert.last_seen_at DESC
                LIMIT 1
                """
            ),
            {
                "batch_id": batch_id,
                "cooldown_minutes": int(settings.stage4_source_unavailable_circuit_cooldown_minutes),
            },
        ).scalar_one_or_none()
    )


def _defer_batch_for_source_unavailable(
    db,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    latest_error: str,
) -> None:
    db.execute(
        text(
            """
            UPDATE stage4_layer_b_batches
            SET status = 'queued',
                latest_error = :latest_error,
                locked_at = NULL,
                heartbeat_at = NULL,
                celery_task_id = NULL,
                updated_at = now()
            WHERE id = :batch_id
            """
        ),
        {"batch_id": int(batch["id"]), "latest_error": latest_error},
    )
    record_event(
        db,
        run_id=int(run["id"]),
        batch_id=int(batch["id"]),
        severity="warning",
        event_type="source_unavailable",
        message="Stage 4 source is temporarily unavailable",
        details={"error": latest_error, "task_name": batch["task_name"]},
    )
    upsert_alert(
        db,
        run_id=int(run["id"]),
        batch_id=None,
        severity="warning",
        alert_type="source_unavailable",
        message="Stage 4 source is temporarily unavailable",
        details={"error": latest_error},
    )
    refresh_run_rollup(db, int(run["id"]))


def _resolve_source_unavailable_alerts(db, *, run_id: int) -> None:
    db.execute(
        text(
            """
            UPDATE stage4_alerts
            SET status = 'resolved',
                resolved_at = COALESCE(resolved_at, now()),
                last_seen_at = now()
            WHERE run_id = :run_id
              AND alert_type = 'source_unavailable'
              AND status <> 'resolved'
            """
        ),
        {"run_id": run_id},
    )


def validate_public_statistics_batch_limits(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> None:
    capabilities = stage4_provider_capabilities(settings)
    if capabilities.adapter != "cdse_public_statistics":
        return

    expected_cells = int(batch["expected_cells"] or 0)
    with _stage4_batch_session() as db:
        actual_cells = _count_batch_cells(db, run, batch)
    batch_cells = max(expected_cells, actual_cells)
    batch_limit = int(settings.stage4_public_statistics_max_cells_per_batch)
    if batch_cells > batch_limit:
        raise RuntimeError(
            f"Public statistics batch {batch['id']} has {batch_cells} cells; "
            f"configured limit is {batch_limit}"
        )

    run_cells = int(run["total_cells"] or 0)
    run_limit = int(settings.stage4_public_statistics_max_cells_per_run)
    hard_ceiling = int(settings.stage4_public_statistics_hard_ceiling_cells)
    if run_cells > hard_ceiling:
        raise RuntimeError(PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER)
    if run_cells > run_limit and not settings.stage4_public_statistics_allow_full_scope:
        raise RuntimeError(
            "Public statistics path is limited to proof runs until full-scope scaling is explicitly enabled"
        )


def validate_public_raster_zonal_batch_limits(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> None:
    capabilities = stage4_provider_capabilities(settings)
    if capabilities.adapter != "public_raster_zonal" or batch["task_name"] != TASK_NDVI_UPDATE:
        return

    run_cells = int(run["total_cells"] or 0)
    source_adapter = public_source_adapter_capabilities(settings)
    if source_adapter.adapter == "cdse_process":
        raise RuntimeError(
            "CDSE Process API source adapter is not allowed for client-approved public Stage 4; "
            "use the public STAC COG source adapter"
        )
    if (
        run_cells > int(settings.stage4_public_statistics_hard_ceiling_cells)
        and not settings.stage4_public_raster_cache_enabled
    ):
        raise RuntimeError("Shared public raster cache is required before full-scope public raster-zonal runs")

    expected_cells = int(batch["expected_cells"] or 0)
    with _stage4_batch_session() as db:
        actual_cells = _count_batch_cells(db, run, batch)
    batch_cells = max(expected_cells, actual_cells)
    batch_limit = int(settings.stage4_public_raster_zonal_max_cells_per_batch)
    if batch_cells > batch_limit:
        raise RuntimeError(
            f"Public raster-zonal batch {batch['id']} has {batch_cells} cells; "
            f"configured limit is {batch_limit}"
        )


def submit_batch_statistical_ndvi_batch(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    import requests

    from app.services.cdse import CdseClient

    start = datetime.combine(run["requested_start"], datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(run["requested_end"], datetime.max.time(), tzinfo=UTC)
    input_uri = _batch_statistical_input_uri(run, batch, settings)
    output_uri = _batch_statistical_output_uri(run, batch, settings)
    client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
    try:
        input_upload = prepare_batch_statistical_input(run, batch, input_uri, settings)
        token = client.get_access_token()
        request = client.create_ndvi_batch_statistics_request(
            token,
            input_features_uri=input_uri,
            output_uri=output_uri,
            start=start,
            end=end,
            max_cloud_coverage=int(run["max_cloud_coverage"]),
            resolution=float(run["resolution"]),
            description=f"Map 1 Turkey Stage 4 NDVI run {run['run_uuid']} batch {batch['batch_index']}",
        )
        request_id = str(request.get("id") or request.get("requestId") or "")
        if not request_id:
            raise RuntimeError("Batch Statistical response did not include request id")
        start_response = client.start_batch_statistics_request(token, request_id)
        status = str(start_response.get("status") or request.get("status") or "SUBMITTED")
    except (requests.RequestException, RuntimeError, KeyError, TypeError, ValueError) as exc:
        result = Stage4BatchResult(
            processed_cells=0,
            succeeded_cells=0,
            failed_cells=int(batch["expected_cells"] or 0),
            skipped_cells=0,
            latest_error=_short_error(exc),
        )
        with _stage4_batch_session() as db:
            _finish_batch(db, int(batch["id"]), "failed", result)
            record_event(
                db,
                run_id=int(run["id"]),
                batch_id=int(batch["id"]),
                severity="critical",
                event_type="batch_statistical_submit_failed",
                message=f"Stage 4 Batch Statistical submit failed for batch {batch['id']}",
                details={"error": result.latest_error},
            )
            upsert_alert(
                db,
                run_id=int(run["id"]),
                batch_id=int(batch["id"]),
                severity="critical",
                alert_type="batch_statistical_submit_failed",
                message=f"Stage 4 Batch Statistical submit failed for batch {batch['id']}",
                details={"error": result.latest_error},
            )
            refresh_run_rollup(db, int(run["id"]))
            db.commit()
        raise

    with _stage4_batch_session() as db:
        metadata = dict(batch["metadata"] or {})
        metadata.update(
            {
                "ndvi_processing_mode": "batch_statistical",
                "batch_statistical_request_id": request_id,
                "batch_statistical_status": status,
                "batch_statistical_input_uri": input_uri,
                "batch_statistical_output_uri": output_uri,
                "batch_statistical_input_upload": input_upload,
            }
        )
        db.execute(
            text(
                """
                UPDATE stage4_layer_b_batches
                SET status = 'external_wait',
                    metadata = CAST(:metadata AS jsonb),
                    heartbeat_at = now(),
                    updated_at = now()
                WHERE id = :batch_id
                """
            ),
            {"batch_id": int(batch["id"]), "metadata": _json(metadata)},
        )
        record_event(
            db,
            run_id=int(run["id"]),
            batch_id=int(batch["id"]),
            severity="info",
            event_type="batch_statistical_submitted",
            message=f"Stage 4 Batch Statistical request submitted for batch {batch['id']}",
            details={
                "request_id": request_id,
                "status": status,
                "input_uri": input_uri,
                "output_uri": output_uri,
            },
        )
        _record_satellite_artifact(
            db,
            run=run,
            batch=batch,
            provider="sentinel_hub_batch_statistical_api",
            artifact_type="input_geopackage",
            storage_uri=input_uri,
            metadata={"request_id": request_id},
        )
        _record_satellite_artifact(
            db,
            run=run,
            batch=batch,
            provider="sentinel_hub_batch_statistical_api",
            artifact_type="output_json_folder",
            storage_uri=output_uri,
            metadata={"request_id": request_id, "status": status},
        )
        refresh_run_rollup(db, int(run["id"]))
        db.commit()
    return {
        "batch_id": int(batch["id"]),
        "status": "external_wait",
        "request_id": request_id,
        "input_uri": input_uri,
        "output_uri": output_uri,
    }


def prepare_batch_statistical_input(
    run: dict[str, Any],
    batch: dict[str, Any],
    input_uri: str,
    settings: Settings,
) -> dict[str, Any]:
    from app.services.stage4_storage import upload_s3_file

    input_path = _batch_statistical_work_path(
        settings,
        run,
        batch,
        "input",
        _batch_statistical_gpkg_name(batch),
    )
    input_path.parent.mkdir(parents=True, exist_ok=True)
    export_batch_statistical_input_gpkg(run, batch, input_path, settings)
    upload_s3_file(input_path, input_uri, settings=settings)
    size_bytes = input_path.stat().st_size
    input_path.unlink(missing_ok=True)
    return {"storage_uri": input_uri, "size_bytes": size_bytes}


def export_batch_statistical_input_gpkg(
    run: dict[str, Any],
    batch: dict[str, Any],
    output_path: Path,
    settings: Settings,
) -> None:
    output_path.unlink(missing_ok=True)
    sql = f"""
        SELECT
            cell.id,
            cell.id::text AS identifier,
            cell.uuid::text AS cell_uuid,
            COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
            cell.geom
        FROM grid_cells cell
        JOIN regions region ON region.id = cell.region_id
        WHERE region.code = '{_sql_literal(str(run["region_code"]))}'
          AND cell.grid_version = '{_sql_literal(str(run["grid_version"]))}'
          AND cell.id BETWEEN {int(batch["grid_cell_id_start"])} AND {int(batch["grid_cell_id_end"])}
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
        "ogr2ogr",
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


def process_public_statistics_ndvi_batch(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Stage4BatchResult:
    import requests

    from app.commands import fetch_cdse_ndvi
    from app.services.cdse import CdseClient

    with _stage4_batch_session() as db:
        cells = _load_batch_cells(db, run, batch)
        batch_wkt = _load_batch_area_wkt(db, run, batch)

    if not cells:
        return Stage4BatchResult(0, 0, 0, 0)

    start = datetime.combine(run["requested_start"], datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(run["requested_end"], datetime.max.time(), tzinfo=UTC)
    client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
    token = None
    products: list[Any] = []
    try:
        token = client.get_access_token()
        products = client.search_sentinel2_products(
            token,
            start=start,
            end=end,
            max_cloud_coverage=int(run["max_cloud_coverage"]),
            area_wkt=batch_wkt,
        )
    except (requests.RequestException, RuntimeError) as exc:
        if not settings.stage4_allow_source_unavailable:
            raise
        return _record_source_unavailable(run, batch, cells, settings, _short_error(exc))

    _record_catalogue_artifacts(run, batch, products, settings)
    processed = succeeded = failed = skipped = 0
    latest_error = None
    with _stage4_batch_session() as db:
        for cell in cells:
            processed += 1
            try:
                statistics = client.request_ndvi_statistics(
                    token,
                    start=start,
                    end=end,
                    max_cloud_coverage=int(run["max_cloud_coverage"]),
                    geometry=cell["geometry"],
                    resolution=float(run["resolution"]),
                )
                inserted = fetch_cdse_ndvi.store_observations(
                    db,
                    products,
                    statistics,
                    cell_id=int(cell["id"]),
                    cell_uuid=str(cell["uuid"]),
                    region_code=str(run["region_code"]),
                    grid_version=str(run["grid_version"]),
                    max_cloud_coverage=int(run["max_cloud_coverage"]),
                    fallback_date=run["requested_end"],
                    min_valid_pixel_ratio=float(run["min_valid_pixel_ratio"]),
                    request_start=start,
                    request_end=end,
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
                    metric_status = sync_latest_ndvi_metric(
                        db,
                        run=run,
                        batch=batch,
                        cell=cell,
                    )
                    if metric_status == "ok":
                        succeeded += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1
            except (requests.RequestException, RuntimeError, KeyError, TypeError, ValueError) as exc:
                latest_error = _short_error(exc)
                fetch_cdse_ndvi.store_unavailable_observation(
                    db,
                    cell_id=int(cell["id"]),
                    cell_uuid=str(cell["uuid"]),
                    region_code=str(run["region_code"]),
                    grid_version=str(run["grid_version"]),
                    max_cloud_coverage=int(run["max_cloud_coverage"]),
                    fallback_date=run["requested_end"],
                    error=latest_error,
                    min_valid_pixel_ratio=float(run["min_valid_pixel_ratio"]),
                    request_start=start,
                    request_end=end,
                    request_resolution=float(run["resolution"]),
                    history_mode=False,
                    pipeline_name=STAGE4_NDVI_PIPELINE_NAME,
                )
                mirror_stage4_observations_to_partitioned(
                    db,
                    grid_cell_id=int(cell["id"]),
                    region_code=str(run["region_code"]),
                    grid_version=str(run["grid_version"]),
                )
                sync_latest_ndvi_metric(db, run=run, batch=batch, cell=cell)
                failed += 1
            if settings.stage4_request_delay_seconds > 0:
                sleep(settings.stage4_request_delay_seconds)
            _heartbeat_batch(db, int(batch["id"]))
        db.commit()
    return Stage4BatchResult(processed, succeeded, failed, skipped, latest_error)


def process_public_raster_zonal_ndvi_batch(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Stage4BatchResult:
    results_path = ensure_public_raster_zonal_results(run, batch, settings)
    manifest = _read_public_raster_zonal_manifest_payload(results_path)
    rows = _read_public_raster_zonal_rows(results_path)
    staged_rows = _normalized_public_raster_zonal_rows(rows, run=run, batch=batch)

    with _stage4_batch_session() as db:
        _stage_public_raster_zonal_rows(db, staged_rows)
        result = _bulk_import_public_raster_zonal_rows(
            db,
            run=run,
            batch=batch,
            results_path=results_path,
            settings=settings,
            manifest=manifest,
        )
        if result.extra_rows:
            record_event(
                db,
                run_id=int(run["id"]),
                batch_id=int(batch["id"]),
                severity="warning",
                event_type="public_raster_zonal_extra_rows",
                message=f"Stage 4 public raster-zonal result had rows outside batch {batch['id']}",
                details={"extra_rows": result.extra_rows, "result_file": str(results_path)},
            )
        _record_satellite_artifact(
            db,
            run=run,
            batch=batch,
            provider="public_raster_zonal",
            artifact_type="zonal_statistics_csv",
            storage_uri=str(results_path),
            metadata={
                "artifact_state": "validated",
                "zonal_state": "zonal_done",
                "import_state": "import_done",
                "validation_state": "validated",
                "rows": len(rows),
                "batching_strategy": (batch.get("metadata") or {}).get("batching_strategy"),
                "stage3_grid_tile_key": (batch.get("metadata") or {}).get("stage3_grid_tile_key"),
                "source_adapter": manifest.get("source_adapter"),
                "source_cache_key": manifest.get("source_cache_key"),
                "shared_cache": manifest.get("shared_cache"),
                "bulk_import": True,
            },
        )
        _heartbeat_batch(db, int(batch["id"]))
        db.commit()
    return result.batch_result


process_ndvi_batch = process_public_statistics_ndvi_batch


def process_biomass_batch(run: dict[str, Any], batch: dict[str, Any], settings: Settings) -> Stage4BatchResult:
    with _stage4_batch_session() as db:
        result = _bulk_import_biomass_rows(db, run=run, batch=batch, settings=settings)
        _heartbeat_batch(db, int(batch["id"]))
        db.commit()
    return result


def _normalized_public_raster_zonal_rows(
    rows: list[dict[str, str]],
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = []
    seen_cell_ids = set()
    duplicate_cell_ids = set()
    for row in rows:
        cell_id = int(row["cell_id"])
        if cell_id in seen_cell_ids:
            duplicate_cell_ids.add(cell_id)
            continue
        seen_cell_ids.add(cell_id)

        metric_date = _parse_metric_date(row.get("metric_date") or row.get("date") or run["requested_end"])
        source_status = _public_raster_zonal_status(row)
        ndvi = _public_raster_zonal_ndvi(row, source_status)
        normalized.append(
            {
                "cell_id": cell_id,
                "metric_date": metric_date,
                "ndvi": ndvi,
                "source_status": source_status,
                "valid_pixel_ratio": _optional_decimal(row.get("valid_pixel_ratio")),
                "sample_count": _optional_int(row.get("sample_count")),
                "valid_pixel_count": _optional_int(row.get("valid_pixel_count")),
                "source_product_id": (
                    row.get("source_product_id")
                    or row.get("product_id")
                    or f"public-raster-zonal-{metric_date.isoformat()}-batch-{int(batch['batch_index']):05d}"
                ),
                "processing_level": row.get("processing_level") or "S2MSI2A",
                "cloud_coverage": _optional_decimal(row.get("cloud_coverage")),
            }
        )
    if duplicate_cell_ids:
        raise RuntimeError("Public raster-zonal result file contains duplicate cell_id rows")
    return normalized


def _stage_public_raster_zonal_rows(db, rows: list[dict[str, Any]]) -> None:
    db.execute(
        text(
            """
            CREATE TEMP TABLE tmp_stage4_public_raster_zonal_rows (
                cell_id BIGINT PRIMARY KEY,
                metric_date DATE NOT NULL,
                ndvi NUMERIC(14, 6),
                source_status TEXT NOT NULL,
                valid_pixel_ratio NUMERIC(8, 6),
                sample_count INTEGER,
                valid_pixel_count INTEGER,
                source_product_id TEXT NOT NULL,
                processing_level TEXT NOT NULL,
                cloud_coverage NUMERIC(5, 2)
            ) ON COMMIT DROP
            """
        )
    )
    for chunk in _chunks(rows, 5000):
        db.execute(
            text(
                """
                INSERT INTO tmp_stage4_public_raster_zonal_rows (
                    cell_id,
                    metric_date,
                    ndvi,
                    source_status,
                    valid_pixel_ratio,
                    sample_count,
                    valid_pixel_count,
                    source_product_id,
                    processing_level,
                    cloud_coverage
                )
                VALUES (
                    :cell_id,
                    :metric_date,
                    :ndvi,
                    :source_status,
                    :valid_pixel_ratio,
                    :sample_count,
                    :valid_pixel_count,
                    :source_product_id,
                    :processing_level,
                    :cloud_coverage
                )
                """
            ),
            chunk,
        )
    db.execute(text("ANALYZE tmp_stage4_public_raster_zonal_rows"))


def _bulk_import_public_raster_zonal_rows(
    db,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    results_path: Path,
    settings: Settings,
    manifest: dict[str, Any] | None = None,
) -> PublicRasterZonalImportResult:
    batch_cells_sql, params = _batch_cells_sql(run, batch)
    db.execute(text(f"CREATE TEMP TABLE tmp_stage4_public_raster_zonal_cells ON COMMIT DROP AS {batch_cells_sql}"), params)
    db.execute(text("CREATE UNIQUE INDEX ON tmp_stage4_public_raster_zonal_cells (id)"))
    db.execute(text("ANALYZE tmp_stage4_public_raster_zonal_cells"))
    batch_cells_sql = "SELECT id, uuid, area_coefficient FROM tmp_stage4_public_raster_zonal_cells"
    source_provenance = _public_raster_zonal_source_provenance(manifest or {})
    import_params = {
        **params,
        "run_id": int(run["id"]),
        "batch_id": int(batch["id"]),
        "region_id": int(run["region_id"]),
        "region_code": str(run["region_code"]),
        "grid_version": str(run["grid_version"]),
        "pipeline_name": STAGE4_NDVI_PIPELINE_NAME,
        "max_cloud_coverage": int(run["max_cloud_coverage"]),
        "min_valid_pixel_ratio": float(run["min_valid_pixel_ratio"]),
        "batching_strategy": (batch.get("metadata") or {}).get("batching_strategy"),
        "stage3_grid_tile_key": (batch.get("metadata") or {}).get("stage3_grid_tile_key"),
        "result_file": str(results_path),
        "source_window_start": run["requested_start"],
        "source_window_end": run["requested_end"],
        "source_provenance": _json(source_provenance),
        "metric_product_prefix": _run_metric_product_prefix(int(run["id"])),
    }
    counts = db.execute(
        text(
            f"""
            WITH batch_cells AS (
                {batch_cells_sql}
            ),
            joined_rows AS (
                SELECT
                    cell.id AS cell_id,
                    stage.source_status
                FROM batch_cells cell
                LEFT JOIN tmp_stage4_public_raster_zonal_rows stage
                  ON stage.cell_id = cell.id
            ),
            extra_rows AS (
                SELECT count(*)::integer AS count
                FROM tmp_stage4_public_raster_zonal_rows stage
                LEFT JOIN batch_cells cell ON cell.id = stage.cell_id
                WHERE cell.id IS NULL
            )
            SELECT
                count(*)::integer AS processed_cells,
                count(*) FILTER (WHERE source_status = 'ok')::integer AS succeeded_cells,
                count(*) FILTER (WHERE source_status IS NOT NULL AND source_status <> 'ok')::integer AS skipped_cells,
                count(*) FILTER (WHERE source_status IS NULL)::integer AS failed_cells,
                (SELECT count FROM extra_rows)::integer AS extra_rows
            FROM joined_rows
            """
        ),
        import_params,
    ).mappings().one()

    db.execute(
        text(
            f"""
            WITH batch_cells AS (
                {batch_cells_sql}
            ),
            source_rows AS (
                SELECT stage.cell_id
                FROM tmp_stage4_public_raster_zonal_rows stage
                JOIN batch_cells cell ON cell.id = stage.cell_id
            )
            UPDATE sentinel_observations observation
            SET is_current = false
            FROM source_rows source
            WHERE observation.grid_cell_id = source.cell_id
              AND observation.grid_cell_id = ANY(ARRAY(
                  SELECT cell_id FROM tmp_stage4_public_raster_zonal_rows
              ))
              AND observation.region_code = :region_code
              AND observation.grid_version = :grid_version
              AND observation.metadata->>'pipeline' = :pipeline_name
              AND observation.is_current
            """
        ),
        import_params,
    )
    db.execute(
        text(
            """
            CREATE TEMP TABLE tmp_stage4_public_raster_zonal_observations (
                id BIGINT PRIMARY KEY,
                grid_cell_id BIGINT NOT NULL,
                acquisition_date DATE NOT NULL,
                ndvi NUMERIC(8, 5),
                cloud_coverage NUMERIC(5, 2),
                processing_level TEXT NOT NULL,
                source_product_id TEXT NOT NULL,
                metadata JSONB NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                is_current BOOLEAN NOT NULL,
                last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
                region_id BIGINT NOT NULL,
                region_code TEXT NOT NULL,
                grid_version TEXT NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    db.execute(
        text(
            f"""
            WITH batch_cells AS (
                {batch_cells_sql}
            ),
            source_rows AS (
                SELECT
                    stage.*,
                    cell.uuid::text AS cell_uuid
                FROM tmp_stage4_public_raster_zonal_rows stage
                JOIN batch_cells cell ON cell.id = stage.cell_id
            ),
            inserted AS (
                INSERT INTO sentinel_observations (
                    grid_cell_id,
                    region_id,
                    region_code,
                    grid_version,
                    acquisition_date,
                    ndvi,
                    cloud_coverage,
                    processing_level,
                    source_product_id,
                    metadata,
                    is_current,
                    last_seen_at
                )
                SELECT
                    source.cell_id,
                    :region_id,
                    :region_code,
                    :grid_version,
                    source.metric_date,
                    source.ndvi,
                    source.cloud_coverage,
                    source.processing_level,
                    source.source_product_id,
                    jsonb_build_object(
                        'pipeline', CAST(:pipeline_name AS text),
                        'region_id', CAST(:region_id AS bigint),
                        'region_code', CAST(:region_code AS text),
                        'grid_version', CAST(:grid_version AS text),
                        'statistics_source', 'public_raster_zonal',
                        'adapter', 'public_raster_zonal',
                        'cell_uuid', source.cell_uuid,
                        'valid_pixel_ratio', source.valid_pixel_ratio,
                        'sample_count', source.sample_count,
                        'valid_pixel_count', source.valid_pixel_count,
                        'max_cloud_coverage', CAST(:max_cloud_coverage AS integer),
                        'min_valid_pixel_ratio', CAST(:min_valid_pixel_ratio AS numeric),
                        'batching_strategy', CAST(:batching_strategy AS text),
                        'stage3_grid_tile_key', CAST(:stage3_grid_tile_key AS text),
                        'result_file', CAST(:result_file AS text),
                        'source_window_start', CAST(:source_window_start AS date),
                        'source_window_end', CAST(:source_window_end AS date),
                        'metric_date_semantics', 'run_requested_end_public_mosaic',
                        'source_provenance', CAST(:source_provenance AS jsonb),
                        'status', source.source_status
                    ),
                    true,
                    now()
                FROM source_rows source
                RETURNING
                    id,
                    grid_cell_id,
                    acquisition_date,
                    ndvi,
                    cloud_coverage,
                    processing_level,
                    source_product_id,
                    metadata,
                    created_at,
                    is_current,
                    last_seen_at,
                    region_id,
                    region_code,
                    grid_version
            )
            INSERT INTO tmp_stage4_public_raster_zonal_observations (
                    id,
                    grid_cell_id,
                    acquisition_date,
                    ndvi,
                    cloud_coverage,
                    processing_level,
                    source_product_id,
                    metadata,
                    created_at,
                    is_current,
                    last_seen_at,
                    region_id,
                    region_code,
                    grid_version
            )
            SELECT
                id,
                grid_cell_id,
                acquisition_date,
                ndvi,
                cloud_coverage,
                processing_level,
                source_product_id,
                metadata,
                created_at,
                is_current,
                last_seen_at,
                region_id,
                region_code,
                grid_version
            FROM inserted
            """
        ),
        import_params,
    )
    db.execute(text("CREATE INDEX ON tmp_stage4_public_raster_zonal_observations (grid_cell_id)"))
    db.execute(text("ANALYZE tmp_stage4_public_raster_zonal_observations"))
    db.execute(
        text(
            f"""
            WITH batch_cells AS (
                {batch_cells_sql}
            ),
            source_rows AS (
                SELECT
                    observation.id AS observation_id,
                    observation.grid_cell_id,
                    observation.acquisition_date,
                    observation.ndvi,
                    observation.source_product_id,
                    stage.source_status,
                    stage.valid_pixel_ratio,
                    cell.area_coefficient
                FROM tmp_stage4_public_raster_zonal_observations observation
                JOIN tmp_stage4_public_raster_zonal_rows stage
                  ON stage.cell_id = observation.grid_cell_id
                JOIN batch_cells cell ON cell.id = observation.grid_cell_id
            )
            INSERT INTO stage4_layer_b_metrics (
                run_id,
                batch_id,
                grid_cell_id,
                region_id,
                region_code,
                grid_version,
                metric_name,
                metric_date,
                value,
                per_hectare_value,
                area_coefficient,
                area_weighted_value,
                unit,
                source_status,
                source_provider,
                source_product_id,
                quality_score,
                metadata
            )
            SELECT
                :run_id,
                :batch_id,
                source.grid_cell_id,
                :region_id,
                :region_code,
                :grid_version,
                'ndvi',
                source.acquisition_date,
                source.ndvi,
                source.ndvi,
                source.area_coefficient,
                CASE
                    WHEN source.ndvi IS NULL THEN NULL
                    ELSE ROUND((source.ndvi * source.area_coefficient)::numeric, 6)
                END,
                'index',
                source.source_status,
                'public_raster_zonal',
                :metric_product_prefix || source.source_product_id,
                source.valid_pixel_ratio,
                jsonb_build_object(
                    'pipeline', CAST(:pipeline_name AS text),
                    'region_id', CAST(:region_id AS bigint),
                    'region_code', CAST(:region_code AS text),
                    'grid_version', CAST(:grid_version AS text),
                    'observation_id', source.observation_id,
                    'source_product_id', source.source_product_id,
                    'statistics_source', 'public_raster_zonal',
                    'adapter', 'public_raster_zonal',
                    'result_file', CAST(:result_file AS text),
                    'source_window_start', CAST(:source_window_start AS date),
                    'source_window_end', CAST(:source_window_end AS date),
                    'metric_date_semantics', 'run_requested_end_public_mosaic',
                    'source_provenance', CAST(:source_provenance AS jsonb)
                )
            FROM source_rows source
            ON CONFLICT (
                grid_cell_id,
                metric_name,
                metric_date,
                source_provider,
                source_product_id
            ) DO UPDATE SET
                batch_id = EXCLUDED.batch_id,
                region_id = EXCLUDED.region_id,
                region_code = EXCLUDED.region_code,
                grid_version = EXCLUDED.grid_version,
                value = EXCLUDED.value,
                per_hectare_value = EXCLUDED.per_hectare_value,
                area_coefficient = EXCLUDED.area_coefficient,
                area_weighted_value = EXCLUDED.area_weighted_value,
                unit = EXCLUDED.unit,
                source_status = EXCLUDED.source_status,
                quality_score = EXCLUDED.quality_score,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            WHERE stage4_layer_b_metrics.run_id = EXCLUDED.run_id
            """
        ),
        import_params,
    )
    if settings.stage4_public_raster_zonal_partitioned_mirror_enabled:
        _mirror_staged_public_raster_zonal_observations_to_partitioned(db, params=import_params)

    failed_cells = int(counts["failed_cells"] or 0)
    latest_error = "Public raster-zonal result is missing for a grid cell" if failed_cells else None
    return PublicRasterZonalImportResult(
        batch_result=Stage4BatchResult(
            processed_cells=int(counts["processed_cells"] or 0),
            succeeded_cells=int(counts["succeeded_cells"] or 0),
            failed_cells=failed_cells,
            skipped_cells=int(counts["skipped_cells"] or 0),
            latest_error=latest_error,
        ),
        extra_rows=int(counts["extra_rows"] or 0),
    )


def _mirror_staged_public_raster_zonal_observations_to_partitioned(
    db,
    *,
    params: dict[str, Any],
) -> int:
    if not _table_exists(db, "sentinel_observations_partitioned"):
        return 0
    rows = db.execute(
        text(
            """
            WITH source_rows AS (
                SELECT
                    staged.id,
                    staged.grid_cell_id,
                    staged.acquisition_date,
                    staged.ndvi,
                    staged.cloud_coverage,
                    staged.processing_level,
                    staged.source_product_id,
                    staged.metadata,
                    staged.created_at,
                    staged.is_current,
                    staged.last_seen_at,
                    staged.region_id,
                    staged.region_code,
                    staged.grid_version
                FROM tmp_stage4_public_raster_zonal_observations staged
            ),
            retired_current AS (
                UPDATE sentinel_observations_partitioned partitioned
                SET is_current = false
                FROM tmp_stage4_public_raster_zonal_rows stage
                WHERE partitioned.grid_cell_id = stage.cell_id
                  AND partitioned.grid_cell_id = ANY(ARRAY(
                      SELECT cell_id FROM tmp_stage4_public_raster_zonal_rows
                  ))
                  AND partitioned.region_id = :region_id
                  AND partitioned.region_code = :region_code
                  AND partitioned.grid_version = :grid_version
                  AND partitioned.metadata->>'pipeline' = :pipeline_name
                  AND partitioned.is_current
                  AND NOT EXISTS (
                        SELECT 1
                        FROM tmp_stage4_public_raster_zonal_observations staged
                        WHERE staged.id = partitioned.id
                  )
                RETURNING partitioned.id
            )
            INSERT INTO sentinel_observations_partitioned (
                id,
                grid_cell_id,
                acquisition_date,
                ndvi,
                cloud_coverage,
                processing_level,
                source_product_id,
                metadata,
                created_at,
                is_current,
                last_seen_at,
                region_id,
                region_code,
                grid_version
            )
            SELECT
                source.id,
                source.grid_cell_id,
                source.acquisition_date,
                source.ndvi,
                source.cloud_coverage,
                source.processing_level,
                source.source_product_id,
                source.metadata,
                source.created_at,
                source.is_current,
                source.last_seen_at,
                source.region_id,
                source.region_code,
                source.grid_version
            FROM source_rows source
            WHERE NOT EXISTS (
                SELECT 1
                FROM sentinel_observations_partitioned partitioned
                WHERE partitioned.id = source.id
                  AND partitioned.region_id = :region_id
            )
            RETURNING id
            """
        ),
        params,
    ).fetchall()
    return len(rows)


def _bulk_import_biomass_rows(
    db,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Stage4BatchResult:
    raise RuntimeError("Private biomass calculation is not distributed.")


def biomass_from_ndvi(ndvi: Any, *, settings: Settings | None = None) -> dict[str, Any]:
    raise RuntimeError("Private biomass calculation is not distributed.")


def record_stage4_ndvi_observation(
    db,
    *,
    run: dict[str, Any],
    cell: dict[str, Any],
    acquisition_date: date,
    ndvi: Any,
    cloud_coverage: Any,
    processing_level: str,
    source_product_id: str,
    source_status: str,
    metadata: dict[str, Any],
) -> int:
    metric_value = Decimal(str(ndvi)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP) if ndvi is not None else None
    db.execute(
        text(
            """
            UPDATE sentinel_observations
            SET is_current = false
            WHERE grid_cell_id = :grid_cell_id
              AND region_code = :region_code
              AND grid_version = :grid_version
              AND metadata->>'pipeline' = :pipeline_name
              AND is_current
            """
        ),
        {
            "grid_cell_id": int(cell["id"]),
            "region_code": str(run["region_code"]),
            "grid_version": str(run["grid_version"]),
            "pipeline_name": STAGE4_NDVI_PIPELINE_NAME,
        },
    )
    return int(
        db.execute(
            text(
                """
                INSERT INTO sentinel_observations (
                    grid_cell_id,
                    region_id,
                    region_code,
                    grid_version,
                    acquisition_date,
                    ndvi,
                    cloud_coverage,
                    processing_level,
                    source_product_id,
                    metadata,
                    is_current,
                    last_seen_at
                )
                VALUES (
                    :grid_cell_id,
                    :region_id,
                    :region_code,
                    :grid_version,
                    :acquisition_date,
                    :ndvi,
                    :cloud_coverage,
                    :processing_level,
                    :source_product_id,
                    CAST(:metadata AS jsonb),
                    true,
                    now()
                )
                RETURNING id
                """
            ),
            {
                "grid_cell_id": int(cell["id"]),
                "region_id": int(run["region_id"]),
                "region_code": str(run["region_code"]),
                "grid_version": str(run["grid_version"]),
                "acquisition_date": acquisition_date,
                "ndvi": metric_value,
                "cloud_coverage": cloud_coverage,
                "processing_level": processing_level,
                "source_product_id": source_product_id or "public-raster-zonal",
                "metadata": _json({**metadata, "status": source_status}),
            },
        ).scalar_one()
    )


def sync_latest_ndvi_metric(
    db,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    cell: dict[str, Any],
) -> str:
    observation = db.execute(
        text(
            """
            SELECT
                id,
                acquisition_date,
                ndvi,
                cloud_coverage,
                source_product_id,
                metadata,
                CASE
                    WHEN metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (metadata->>'valid_pixel_ratio')::numeric
                END AS valid_pixel_ratio,
                COALESCE(
                    metadata->>'status',
                    CASE WHEN ndvi IS NOT NULL THEN 'ok' END,
                    'unknown'
                ) AS source_status
            FROM sentinel_observations
            WHERE grid_cell_id = :grid_cell_id
              AND region_code = :region_code
              AND grid_version = :grid_version
              AND metadata->>'pipeline' = :pipeline_name
            ORDER BY acquisition_date DESC, id DESC
            LIMIT 1
            """
        ),
        {
            "grid_cell_id": int(cell["id"]),
            "region_code": str(run["region_code"]),
            "grid_version": str(run["grid_version"]),
            "pipeline_name": STAGE4_NDVI_PIPELINE_NAME,
        },
    ).mappings().one_or_none()
    if observation is None:
        return "missing_observation"
    record_layer_b_metric(
        db,
        run_id=int(run["id"]),
        batch_id=int(batch["id"]),
        grid_cell_id=int(cell["id"]),
        region_id=int(run["region_id"]),
        region_code=str(run["region_code"]),
        grid_version=str(run["grid_version"]),
        metric_name="ndvi",
        metric_date=observation["acquisition_date"],
        value=observation["ndvi"],
        unit="index",
        source_status=str(observation["source_status"]),
        source_provider="cdse_public_statistics_api",
        source_product_id=str(observation["source_product_id"]),
        area_coefficient=cell["area_coefficient"],
        quality_score=observation["valid_pixel_ratio"],
        metadata={
            "pipeline": STAGE4_NDVI_PIPELINE_NAME,
            "observation_id": int(observation["id"]),
            "cloud_coverage": _decimal_to_float(observation["cloud_coverage"]),
            "observation_metadata": observation["metadata"],
        },
    )
    return str(observation["source_status"])


def mirror_stage4_observations_to_partitioned(
    db,
    *,
    grid_cell_id: int,
    region_code: str,
    grid_version: str,
) -> int:
    if not _table_exists(db, "sentinel_observations_partitioned"):
        return 0
    rows = db.execute(
        text(
            """
            WITH source_rows AS (
                SELECT
                    id,
                    grid_cell_id,
                    acquisition_date,
                    ndvi,
                    cloud_coverage,
                    processing_level,
                    source_product_id,
                    metadata,
                    created_at,
                    is_current,
                    last_seen_at,
                    region_id,
                    region_code,
                    grid_version
                FROM sentinel_observations
                WHERE grid_cell_id = :grid_cell_id
                  AND region_code = :region_code
                  AND grid_version = :grid_version
                  AND metadata->>'pipeline' = :pipeline_name
            ),
            retired AS (
                UPDATE sentinel_observations_partitioned partitioned
                SET is_current = source_rows.is_current
                FROM source_rows
                WHERE partitioned.id = source_rows.id
                  AND partitioned.region_id = source_rows.region_id
                  AND partitioned.is_current IS DISTINCT FROM source_rows.is_current
                RETURNING partitioned.id
            )
            INSERT INTO sentinel_observations_partitioned (
                id,
                grid_cell_id,
                acquisition_date,
                ndvi,
                cloud_coverage,
                processing_level,
                source_product_id,
                metadata,
                created_at,
                is_current,
                last_seen_at,
                region_id,
                region_code,
                grid_version
            )
            SELECT
                observation.id,
                observation.grid_cell_id,
                observation.acquisition_date,
                observation.ndvi,
                observation.cloud_coverage,
                observation.processing_level,
                observation.source_product_id,
                observation.metadata,
                observation.created_at,
                observation.is_current,
                observation.last_seen_at,
                observation.region_id,
                observation.region_code,
                observation.grid_version
            FROM source_rows observation
            WHERE NOT EXISTS (
                SELECT 1
                FROM sentinel_observations_partitioned partitioned
                WHERE partitioned.id = observation.id
                  AND partitioned.region_id = observation.region_id
            )
            RETURNING id
            """
        ),
        {
            "grid_cell_id": grid_cell_id,
            "region_code": region_code,
            "grid_version": grid_version,
            "pipeline_name": STAGE4_NDVI_PIPELINE_NAME,
        },
    ).fetchall()
    return len(rows)


def _batch_waits_for_ndvi(db, batch_id: int) -> bool:
    row = db.execute(
        text(
            """
            SELECT run_id, task_name, batch_index, status
            FROM stage4_layer_b_batches
            WHERE id = :batch_id
            """
        ),
        {"batch_id": batch_id},
    ).mappings().one_or_none()
    if row is None:
        return False
    if row["task_name"] != TASK_BIOMASS_UPDATE or row["status"] in TERMINAL_BATCH_STATUSES:
        return False
    ndvi_status = db.execute(
        text(
            """
            SELECT status
            FROM stage4_layer_b_batches
            WHERE run_id = :run_id
              AND task_name = :ndvi_task_name
              AND batch_index = :batch_index
            """
        ),
        {
            "run_id": int(row["run_id"]),
            "ndvi_task_name": TASK_NDVI_UPDATE,
            "batch_index": int(row["batch_index"]),
        },
    ).scalar_one_or_none()
    return ndvi_status is not None and ndvi_status not in TERMINAL_BATCH_STATUSES


def _run_metric_product_prefix(run_id: int) -> str:
    # The existing unique key has no run_id. Scope every metric writer, including
    # legacy/provider imports, without changing the underlying observation ID.
    return f"stage4-run-{int(run_id)}:"


def record_layer_b_metric(
    db,
    *,
    run_id: int,
    batch_id: int,
    grid_cell_id: int,
    region_id: int,
    region_code: str,
    grid_version: str,
    metric_name: str,
    metric_date: date,
    value: Any,
    unit: str,
    source_status: str,
    source_provider: str,
    source_product_id: str,
    area_coefficient: Any,
    quality_score: Any,
    metadata: dict[str, Any],
) -> None:
    area_coefficient = area_coefficient_from_area_ha(area_coefficient)
    metric_value = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP) if value is not None else None
    area_weighted_value = (
        (metric_value * area_coefficient).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if metric_value is not None
        else None
    )
    db.execute(
        text(
            """
            INSERT INTO stage4_layer_b_metrics (
                run_id,
                batch_id,
                grid_cell_id,
                region_id,
                region_code,
                grid_version,
                metric_name,
                metric_date,
                value,
                per_hectare_value,
                area_coefficient,
                area_weighted_value,
                unit,
                source_status,
                source_provider,
                source_product_id,
                quality_score,
                metadata
            )
            VALUES (
                :run_id,
                :batch_id,
                :grid_cell_id,
                :region_id,
                :region_code,
                :grid_version,
                :metric_name,
                :metric_date,
                :value,
                :per_hectare_value,
                :area_coefficient,
                :area_weighted_value,
                :unit,
                :source_status,
                :source_provider,
                :source_product_id,
                :quality_score,
                CAST(:metadata AS jsonb)
            )
            ON CONFLICT (
                grid_cell_id,
                metric_name,
                metric_date,
                source_provider,
                source_product_id
            ) DO UPDATE SET
                batch_id = EXCLUDED.batch_id,
                region_id = EXCLUDED.region_id,
                region_code = EXCLUDED.region_code,
                grid_version = EXCLUDED.grid_version,
                value = EXCLUDED.value,
                per_hectare_value = EXCLUDED.per_hectare_value,
                area_coefficient = EXCLUDED.area_coefficient,
                area_weighted_value = EXCLUDED.area_weighted_value,
                unit = EXCLUDED.unit,
                source_status = EXCLUDED.source_status,
                quality_score = EXCLUDED.quality_score,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            WHERE stage4_layer_b_metrics.run_id = EXCLUDED.run_id
            """
        ),
        {
            "run_id": run_id,
            "batch_id": batch_id,
            "grid_cell_id": grid_cell_id,
            "region_id": region_id,
            "region_code": region_code,
            "grid_version": grid_version,
            "metric_name": metric_name,
            "metric_date": metric_date,
            "value": metric_value,
            "per_hectare_value": metric_value,
            "area_coefficient": area_coefficient,
            "area_weighted_value": area_weighted_value,
            "unit": unit,
            "source_status": source_status,
            "source_provider": source_provider,
            "source_product_id": _run_metric_product_prefix(run_id) + (source_product_id or "none"),
            "quality_score": quality_score,
            "metadata": _json({**metadata, "source_product_id": source_product_id or "none"}),
        },
    )


def backfill_area_coefficients(
    db,
    *,
    table_name: str,
    region_code: str | None = None,
    grid_version: str | None = None,
    batch_size: int = 100_000,
    max_batches: int | None = None,
) -> dict[str, Any]:
    _ensure_grid_table(table_name)
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    region_id = _region_id_for_code(db, region_code) if region_code else None
    updated = 0
    batches = 0
    span = _area_coefficient_missing_span(
        db,
        table_name=table_name,
        region_id=region_id,
        grid_version=grid_version,
    )
    if span["min_id"] is None or span["max_id"] is None:
        return {
            "table": table_name,
            "updated": 0,
            "batches": 0,
            "remaining": 0,
        }
    start_id = int(span["min_id"])
    max_id = int(span["max_id"])
    where_sql, static_params = _area_coefficient_backfill_filters(region_id, grid_version)
    while max_batches is None or batches < max_batches:
        if start_id > max_id:
            break
        end_id = min(start_id + batch_size - 1, max_id)
        params = {
            "start_id": start_id,
            "end_id": end_id,
            **static_params,
        }
        result = db.execute(
            text(
                _area_coefficient_backfill_sql(table_name, where_sql=where_sql)
            ),
            params,
        )
        changed = max(result.rowcount or 0, 0)
        updated += changed
        batches += 1
        db.commit()
        start_id = end_id + 1
    remaining = db.execute(
        text(
            f"""
            SELECT count(*)::bigint
            FROM {table_name} cell
            WHERE cell.area_coefficient IS NULL
              AND (CAST(:region_id AS bigint) IS NULL OR cell.region_id = CAST(:region_id AS bigint))
              AND (CAST(:grid_version AS text) IS NULL OR cell.grid_version = CAST(:grid_version AS text))
            """
        ),
        {"region_id": region_id, "grid_version": grid_version},
    ).scalar_one()
    return {
        "table": table_name,
        "updated": updated,
        "batches": batches,
        "remaining": int(remaining or 0),
    }


def _region_id_for_code(db, region_code: str) -> int:
    region_id = db.execute(
        text("SELECT id FROM regions WHERE code = :region_code"),
        {"region_code": region_code},
    ).scalar_one_or_none()
    if region_id is None:
        raise RuntimeError(f"region {region_code} does not exist")
    return int(region_id)


def _area_coefficient_missing_span(
    db,
    *,
    table_name: str,
    region_id: int | None,
    grid_version: str | None,
) -> dict[str, int | None]:
    return dict(
        db.execute(
            text(
                f"""
                SELECT
                    min(cell.id)::bigint AS min_id,
                    max(cell.id)::bigint AS max_id
                FROM {table_name} cell
                WHERE cell.area_coefficient IS NULL
                  AND (CAST(:region_id AS bigint) IS NULL OR cell.region_id = CAST(:region_id AS bigint))
                  AND (CAST(:grid_version AS text) IS NULL OR cell.grid_version = CAST(:grid_version AS text))
                """
            ),
            {"region_id": region_id, "grid_version": grid_version},
        ).mappings().one()
    )


def _area_coefficient_backfill_filters(
    region_id: int | None,
    grid_version: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses = ["cell.area_coefficient IS NULL", "cell.id BETWEEN :start_id AND :end_id"]
    params: dict[str, Any] = {}
    if region_id is not None:
        clauses.append("cell.region_id = :region_id")
        params["region_id"] = region_id
    if grid_version is not None:
        clauses.append("cell.grid_version = :grid_version")
        params["grid_version"] = grid_version
    return " AND ".join(clauses), params


def _area_coefficient_backfill_sql(table_name: str, *, where_sql: str | None = None) -> str:
    where_sql = where_sql or (
        "cell.area_coefficient IS NULL "
        "AND (CAST(:region_id AS bigint) IS NULL OR cell.region_id = CAST(:region_id AS bigint)) "
        "AND (CAST(:grid_version AS text) IS NULL OR cell.grid_version = CAST(:grid_version AS text)) "
        "AND cell.id BETWEEN :start_id AND :end_id"
    )
    if table_name == "grid_cells_partitioned":
        return f"""
            UPDATE {table_name} cell
            SET area_coefficient = COALESCE(
                active.area_coefficient,
                ROUND(active.area_ha::numeric, 6),
                ROUND(cell.area_ha::numeric, 6)
            )
            FROM grid_cells active
            WHERE active.id = cell.id
              AND {where_sql}
        """
    return f"""
        UPDATE {table_name} cell
        SET area_coefficient = ROUND(cell.area_ha::numeric, 6)
        WHERE {where_sql}
    """


def detect_stuck_batches(db, *, stuck_after_minutes: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            WITH stale AS (
                SELECT *
                FROM stage4_layer_b_batches
                WHERE status = 'running'
                  AND COALESCE(heartbeat_at, started_at, updated_at, created_at)
                      < now() - (:stuck_after_minutes * INTERVAL '1 minute')
                  AND pg_try_advisory_xact_lock(
                      hashtextextended('map1:stage4:batch:' || CAST(id AS text), 0)
                  )
            )
            UPDATE stage4_layer_b_batches batch
            SET status = 'stuck',
                latest_error = 'Batch heartbeat is stale',
                updated_at = now()
            FROM stale
            WHERE batch.id = stale.id
              AND batch.status = 'running'
              AND COALESCE(batch.heartbeat_at, batch.started_at, batch.updated_at, batch.created_at)
                  < now() - (:stuck_after_minutes * INTERVAL '1 minute')
            RETURNING batch.*
            """
        ),
        {"stuck_after_minutes": stuck_after_minutes},
    ).mappings().all()
    stuck = [dict(row) for row in rows]
    for batch in stuck:
        upsert_alert(
            db,
            run_id=int(batch["run_id"]),
            batch_id=int(batch["id"]),
            severity="critical",
            alert_type="batch_stuck",
            message=f"Stage 4 batch {batch['id']} heartbeat is stale",
            details={
                "task_name": batch["task_name"],
                "stuck_after_minutes": stuck_after_minutes,
            },
        )
        record_event(
            db,
            run_id=int(batch["run_id"]),
            batch_id=int(batch["id"]),
            severity="critical",
            event_type="batch_stuck",
            message=f"Stage 4 batch {batch['id']} heartbeat is stale",
            details={
                "task_name": batch["task_name"],
                "stuck_after_minutes": stuck_after_minutes,
            },
        )
    for run_id in sorted({int(batch["run_id"]) for batch in stuck}):
        refresh_run_rollup(db, run_id)
    return stuck


def poll_batch_statistical_requests(db, settings: Settings | None = None, limit: int = 50) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    from app.services.cdse import CdseClient

    rows = db.execute(
        text(
            """
            SELECT
                batch.*,
                run.run_uuid,
                run.region_id,
                run.region_code,
                run.grid_version,
                run.requested_end
            FROM stage4_layer_b_batches batch
            JOIN stage4_layer_b_runs run ON run.id = batch.run_id
            WHERE batch.status = 'external_wait'
              AND batch.metadata ? 'batch_statistical_request_id'
            ORDER BY batch.updated_at, batch.id
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    batches = [dict(row) for row in rows]
    if not batches:
        return []

    client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
    token = client.get_access_token()
    results = []
    for batch in batches:
        request_id = str(batch["metadata"]["batch_statistical_request_id"])
        status_info = client.get_batch_statistics_request(token, request_id)
        status = str(status_info.get("status") or status_info.get("requestStatus") or "UNKNOWN")
        metadata = dict(batch["metadata"] or {})
        metadata["batch_statistical_status"] = status
        metadata["batch_statistical_status_response"] = _redact_sensitive_metadata(status_info)
        normalized_status = status.upper()
        failed_external = normalized_status in {"FAILED", "STOPPED", "CANCELED", "CANCELLED"}
        partial_external = normalized_status == "PARTIAL"
        ready_external = normalized_status in {"DONE", "PARTIAL"}
        next_status = "external_ready" if ready_external else "external_wait"
        if failed_external:
            next_status = "failed"
            metadata["latest_error"] = f"Batch Statistical request {request_id} is {status}"
        if partial_external:
            metadata["latest_warning"] = f"Batch Statistical request {request_id} is {status}"
        db.execute(
            text(
                """
                UPDATE stage4_layer_b_batches
                SET status = :status,
                    processed_cells = CASE WHEN :failed_external THEN 0 ELSE processed_cells END,
                    succeeded_cells = CASE WHEN :failed_external THEN 0 ELSE succeeded_cells END,
                    failed_cells = CASE WHEN :failed_external THEN expected_cells ELSE failed_cells END,
                    skipped_cells = CASE WHEN :failed_external THEN 0 ELSE skipped_cells END,
                    latest_error = CASE WHEN :failed_external THEN :latest_error ELSE latest_error END,
                    finished_at = CASE WHEN :failed_external THEN now() ELSE finished_at END,
                    metadata = CAST(:metadata AS jsonb),
                    heartbeat_at = now(),
                    updated_at = now()
                WHERE id = :batch_id
                """
            ),
            {
                "batch_id": int(batch["id"]),
                "status": next_status,
                "failed_external": failed_external,
                "latest_error": metadata.get("latest_error"),
                "metadata": _json(metadata),
            },
        )
        if failed_external:
            upsert_alert(
                db,
                run_id=int(batch["run_id"]),
                batch_id=int(batch["id"]),
                severity="critical",
                alert_type="batch_statistical_failed",
                message=f"Stage 4 Batch Statistical request {request_id} is {status}",
                details={"request_id": request_id, "status": status},
            )
            record_event(
                db,
                run_id=int(batch["run_id"]),
                batch_id=int(batch["id"]),
                severity="critical",
                event_type="batch_statistical_failed",
                message=f"Stage 4 Batch Statistical request {request_id} is {status}",
                details={"request_id": request_id, "status": status},
            )
        if partial_external:
            upsert_alert(
                db,
                run_id=int(batch["run_id"]),
                batch_id=int(batch["id"]),
                severity="warning",
                alert_type="batch_statistical_partial",
                message=f"Stage 4 Batch Statistical request {request_id} is PARTIAL",
                details={"request_id": request_id, "status": status},
            )
        if next_status == "external_ready":
            record_event(
                db,
                run_id=int(batch["run_id"]),
                batch_id=int(batch["id"]),
                severity="warning" if partial_external else "info",
                event_type="batch_statistical_ready",
                message=f"Stage 4 Batch Statistical outputs are ready for batch {batch['id']}",
                details={"request_id": request_id, "status": status},
            )
        results.append(
            {
                "batch_id": int(batch["id"]),
                "request_id": request_id,
                "status": status,
                "batch_status": next_status,
            }
        )

    for run_id in sorted({int(batch["run_id"]) for batch in batches}):
        refresh_run_rollup(db, run_id)
    return results


def import_batch_statistical_ndvi_batch(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    from app.commands import fetch_cdse_ndvi
    from app.services.stage4_storage import download_s3_prefix

    metadata = dict(batch["metadata"] or {})
    output_uri = str(metadata.get("batch_statistical_output_uri") or _batch_statistical_output_uri(run, batch, settings))
    output_dir = _batch_statistical_work_path(settings, run, batch, "output")
    shutil.rmtree(output_dir, ignore_errors=True)
    downloaded = download_s3_prefix(output_uri, output_dir, settings=settings, suffix=".json")
    if not downloaded:
        raise RuntimeError(f"No Batch Statistical JSON outputs found under {output_uri}")

    imported = failed = skipped = 0
    latest_error = None
    with _stage4_batch_session() as db:
        for path in sorted(downloaded):
            try:
                payload = json.loads(path.read_text())
                statistics = _batch_statistical_statistics_payload(payload)
                cell_id = _batch_statistical_cell_id(payload)
                if cell_id is None:
                    skipped += 1
                    continue
                cell = _load_stage4_cell(db, int(cell_id), run)
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
                latest_error = _short_error(exc)

        processed = imported + failed + skipped
        missing = max(int(batch["expected_cells"] or 0) - processed, 0)
        if missing:
            failed += missing
            latest_error = latest_error or f"{missing} Batch Statistical outputs were missing"
        processed = imported + failed + skipped
        status = "completed"
        if failed and imported:
            status = "completed_with_errors"
        elif failed and not imported:
            status = "failed"
        _finish_batch(
            db,
            int(batch["id"]),
            status,
            Stage4BatchResult(
                processed_cells=processed,
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
            details={
                "output_uri": output_uri,
                "downloaded_files": len(downloaded),
                "imported": imported,
                "failed": failed,
                "skipped": skipped,
            },
        )
        refresh_run_rollup(db, int(run["id"]))
        db.commit()
    shutil.rmtree(output_dir, ignore_errors=True)
    return {
        "batch_id": int(batch["id"]),
        "status": status,
        "downloaded_files": len(downloaded),
        "imported": imported,
        "failed": failed,
        "skipped": skipped,
    }


def upsert_alert(
    db,
    *,
    run_id: int | None,
    batch_id: int | None,
    severity: str,
    alert_type: str,
    message: str,
    details: dict[str, Any],
) -> None:
    existing_id = db.execute(
        text(
            """
            SELECT id
            FROM stage4_alerts
            WHERE status <> 'resolved'
              AND alert_type = :alert_type
              AND (
                    (batch_id IS NOT DISTINCT FROM CAST(:batch_id AS bigint))
                AND (run_id IS NOT DISTINCT FROM CAST(:run_id AS bigint))
              )
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"run_id": run_id, "batch_id": batch_id, "alert_type": alert_type},
    ).scalar_one_or_none()
    if existing_id:
        db.execute(
            text(
                """
                UPDATE stage4_alerts
                SET severity = :severity,
                    message = :message,
                    details = CAST(:details AS jsonb),
                    last_seen_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": existing_id,
                "severity": severity,
                "message": message,
                "details": _json(details),
            },
        )
        return
    db.execute(
        text(
            """
            INSERT INTO stage4_alerts (
                run_id,
                batch_id,
                severity,
                alert_type,
                message,
                details
            )
            VALUES (
                :run_id,
                :batch_id,
                :severity,
                :alert_type,
                :message,
                CAST(:details AS jsonb)
            )
            """
        ),
        {
            "run_id": run_id,
            "batch_id": batch_id,
            "severity": severity,
            "alert_type": alert_type,
            "message": message,
            "details": _json(details),
        },
    )


def record_event(
    db,
    *,
    run_id: int | None,
    batch_id: int | None,
    severity: str,
    event_type: str,
    message: str,
    details: dict[str, Any],
) -> None:
    db.execute(
        text(
            """
            INSERT INTO stage4_layer_b_events (
                run_id,
                batch_id,
                severity,
                event_type,
                message,
                details
            )
            VALUES (
                :run_id,
                :batch_id,
                :severity,
                :event_type,
                :message,
                CAST(:details AS jsonb)
            )
            """
        ),
        {
            "run_id": run_id,
            "batch_id": batch_id,
            "severity": severity,
            "event_type": event_type,
            "message": message,
            "details": _json(details),
        },
    )


def record_heartbeat(
    db,
    *,
    service_name: str,
    worker_name: str,
    status: str,
    run_id: int | None = None,
    batch_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO stage4_process_heartbeats (
                service_name,
                worker_name,
                pid,
                run_id,
                batch_id,
                status,
                metadata,
                heartbeat_at
            )
            VALUES (
                :service_name,
                :worker_name,
                :pid,
                :run_id,
                :batch_id,
                :status,
                CAST(:metadata AS jsonb),
                now()
            )
            ON CONFLICT (service_name, worker_name) DO UPDATE SET
                pid = EXCLUDED.pid,
                run_id = EXCLUDED.run_id,
                batch_id = EXCLUDED.batch_id,
                status = EXCLUDED.status,
                metadata = EXCLUDED.metadata,
                heartbeat_at = now()
            """
        ),
        {
            "service_name": service_name,
            "worker_name": worker_name,
            "pid": os.getpid(),
            "run_id": run_id,
            "batch_id": batch_id,
            "status": status,
            "metadata": _json(metadata or {}),
        },
    )


def refresh_run_rollup(db, run_id: int) -> None:
    db.execute(
        text(
            """
            WITH rollup AS (
                SELECT
                    count(*)::integer AS total_batches,
                    count(*) FILTER (WHERE status = 'queued')::integer AS queued_batches,
                    COALESCE(sum(processed_cells), 0)::bigint AS processed_cells,
                    COALESCE(sum(succeeded_cells), 0)::bigint AS succeeded_cells,
                    COALESCE(sum(failed_cells), 0)::bigint AS failed_cells,
                    COALESCE(sum(skipped_cells), 0)::bigint AS skipped_cells,
                    count(*) FILTER (WHERE status IN ('failed', 'stuck'))::integer AS failed_batches,
                    count(*) FILTER (WHERE status = 'cancelled')::integer AS cancelled_batches,
                    count(*) FILTER (WHERE status = 'completed_with_errors')::integer AS warning_batches,
                    count(*) FILTER (WHERE status = 'running')::integer AS running_batches,
                    count(*) FILTER (WHERE status IN ('external_wait', 'external_ready'))::integer AS external_batches,
                    count(*) FILTER (
                        WHERE status NOT IN (
                            'completed',
                            'completed_with_errors',
                            'failed',
                            'cancelled',
                            'skipped',
                            'stuck'
                        )
                    )::integer AS open_batches,
                    max(latest_error) FILTER (WHERE latest_error IS NOT NULL) AS latest_error,
                    max(started_at) FILTER (WHERE started_at IS NOT NULL) AS last_started_at,
                    max(finished_at) FILTER (WHERE finished_at IS NOT NULL) AS last_finished_at
                FROM stage4_layer_b_batches
                WHERE run_id = :run_id
            )
            UPDATE stage4_layer_b_runs run
            SET total_batches = rollup.total_batches,
                queued_batches = rollup.queued_batches,
                processed_cells = rollup.processed_cells,
                succeeded_cells = rollup.succeeded_cells,
                failed_cells = rollup.failed_cells,
                skipped_cells = rollup.skipped_cells,
                latest_error = rollup.latest_error,
                started_at = COALESCE(run.started_at, rollup.last_started_at),
                finished_at = CASE
                    WHEN rollup.open_batches = 0 THEN COALESCE(run.finished_at, now())
                    ELSE run.finished_at
                END,
                status = CASE
                    WHEN rollup.open_batches = 0 AND rollup.failed_batches > 0 THEN 'failed'
                    WHEN rollup.open_batches = 0 AND rollup.cancelled_batches > 0 THEN 'cancelled'
                    WHEN rollup.open_batches = 0 AND rollup.warning_batches > 0 THEN 'completed_with_errors'
                    WHEN rollup.open_batches = 0 THEN 'completed'
                    WHEN rollup.running_batches > 0 THEN 'running'
                    WHEN rollup.external_batches > 0 THEN 'external_wait'
                    ELSE 'queued'
                END,
                updated_at = now()
            FROM rollup
            WHERE run.id = :run_id
            """
        ),
        {"run_id": run_id},
    )


def _claim_batch(db, batch_id: int) -> dict[str, Any] | None:
    lease = _stage4_batch_lease.get()
    if lease is not None:
        lease.assert_owned_connection()
        if lease.batch_id != int(batch_id) or db.get_bind() is not lease.connection:
            raise Stage4BatchLeaseLost("Stage 4 claim is not using its batch lock connection")
    row = db.execute(
        text(
            """
            SELECT *
            FROM stage4_layer_b_batches
            WHERE id = :batch_id
            FOR UPDATE
            """
        ),
        {"batch_id": batch_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    # Only the holder of the session lock can prove an earlier delivery is gone.
    # Heartbeat age alone never authorizes taking a live worker's batch.
    claimable = {"queued", "external_ready"}
    if lease is not None:
        claimable.update({"running", "stuck"})
    if row["status"] not in claimable:
        return None
    attempts = db.execute(
        text(
            """
            UPDATE stage4_layer_b_batches
            SET status = 'running',
                attempts = attempts + 1,
                locked_at = now(),
                started_at = COALESCE(started_at, now()),
                heartbeat_at = now(),
                updated_at = now()
            WHERE id = :batch_id
            RETURNING attempts
            """
        ),
        {"batch_id": batch_id},
    ).scalar_one()
    record_heartbeat(
        db,
        service_name="stage4-worker",
        worker_name=f"pid-{os.getpid()}",
        status="running",
        run_id=int(row["run_id"]),
        batch_id=batch_id,
    )
    refresh_run_rollup(db, int(row["run_id"]))
    return {**dict(row), "attempts": int(attempts)}


def _finish_batch(db, batch_id: int, status: str, result: Stage4BatchResult) -> None:
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


def _load_run(db, run_id: int) -> dict[str, Any]:
    row = db.execute(
        text("SELECT * FROM stage4_layer_b_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"Stage 4 run not found: {run_id}")
    return dict(row)


def _load_batch_cells(
    db,
    run: dict[str, Any],
    batch: dict[str, Any],
    *,
    include_geometry: bool = True,
) -> list[dict[str, Any]]:
    geometry_sql = "ST_AsGeoJSON(cell.geom)::json AS geometry," if include_geometry else ""
    table_name = _stage4_grid_cells_table(run)
    spatial_join_sql, spatial_where_sql, spatial_params = _spatial_tile_batch_sql(batch)
    rows = db.execute(
        text(
            f"""
            SELECT
                cell.id,
                cell.uuid::text AS uuid,
                cell.area_ha,
                COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
                {geometry_sql}
                cell.grid_version
            FROM {table_name} cell
            {spatial_join_sql}
            WHERE cell.region_id = :region_id
              AND cell.grid_version = :grid_version
              AND cell.id BETWEEN :start_id AND :end_id
              {spatial_where_sql}
            ORDER BY cell.id
            """
        ),
        {
            "region_id": int(run["region_id"]),
            "grid_version": str(run["grid_version"]),
            "start_id": int(batch["grid_cell_id_start"]),
            "end_id": int(batch["grid_cell_id_end"]),
            **spatial_params,
        },
    ).mappings().all()
    cells = [dict(row) for row in rows]
    _validate_spatial_tile_cell_count(batch, len(cells))
    return cells


def _batch_cells_sql(run: dict[str, Any], batch: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    table_name = _stage4_grid_cells_table(run)
    spatial_join_sql, spatial_where_sql, spatial_params = _spatial_tile_batch_sql(batch)
    sql = f"""
        SELECT
            cell.id,
            cell.uuid,
            COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient
        FROM {table_name} cell
        {spatial_join_sql}
        WHERE cell.region_id = :region_id
          AND cell.grid_version = :grid_version
          AND cell.id BETWEEN :start_id AND :end_id
          {spatial_where_sql}
    """
    return sql, {
        "region_id": int(run["region_id"]),
        "grid_version": str(run["grid_version"]),
        "start_id": int(batch["grid_cell_id_start"]),
        "end_id": int(batch["grid_cell_id_end"]),
        **spatial_params,
    }


def _count_batch_cells(db, run: dict[str, Any], batch: dict[str, Any]) -> int:
    table_name = _stage4_grid_cells_table(run)
    spatial_join_sql, spatial_where_sql, spatial_params = _spatial_tile_batch_sql(batch)
    count = int(
        db.execute(
            text(
                f"""
                SELECT count(*)::integer
                FROM {table_name} cell
                {spatial_join_sql}
                WHERE cell.region_id = :region_id
                  AND cell.grid_version = :grid_version
                  AND cell.id BETWEEN :start_id AND :end_id
                  {spatial_where_sql}
                """
            ),
            {
                "region_id": int(run["region_id"]),
                "grid_version": str(run["grid_version"]),
                "start_id": int(batch["grid_cell_id_start"]),
                "end_id": int(batch["grid_cell_id_end"]),
                **spatial_params,
            },
        ).scalar_one()
        or 0
    )
    _validate_spatial_tile_cell_count(batch, count)
    return count


def _stage4_grid_cells_table(run: dict[str, Any]) -> str:
    return grid_cells_table_sql(
        {
            "region_code": str(run["region_code"]),
            "grid_version": str(run["grid_version"]),
        }
    )


def _validate_spatial_tile_cell_count(batch: dict[str, Any], actual_cells: int) -> None:
    if not _is_stage3_spatial_tile_batch(batch):
        return
    expected_cells = int(batch["expected_cells"] or 0)
    if actual_cells != expected_cells:
        raise RuntimeError(
            "Stage 4 spatial tile cell filter mismatch: "
            f"batch {batch['id']} expected {expected_cells}, got {actual_cells}"
        )


def _load_batch_area_wkt(db, run: dict[str, Any], batch: dict[str, Any]) -> str:
    if _is_stage3_spatial_tile_batch(batch):
        params = _spatial_tile_batch_params(batch)
        return str(
            db.execute(
                text(
                    """
                    SELECT ST_AsText(
                        ST_Transform(
                            ST_MakeEnvelope(:tile_min_x, :tile_min_y, :tile_max_x, :tile_max_y, :tile_projected_srid),
                            4326
                        )
                    )
                    """
                ),
                params,
            ).scalar_one()
        )

    wkt = db.execute(
        text(
            """
            SELECT ST_AsText(ST_Envelope(ST_Collect(cell.geom)))
            FROM grid_cells cell
            WHERE cell.region_id = :region_id
              AND cell.grid_version = :grid_version
              AND cell.id BETWEEN :start_id AND :end_id
            """
        ),
        {
            "region_id": int(run["region_id"]),
            "grid_version": str(run["grid_version"]),
            "start_id": int(batch["grid_cell_id_start"]),
            "end_id": int(batch["grid_cell_id_end"]),
        },
    ).scalar_one_or_none()
    if not wkt:
        raise RuntimeError(f"Stage 4 batch has no grid cells: {batch['id']}")
    return str(wkt)


def _is_stage3_spatial_tile_batch(batch: dict[str, Any]) -> bool:
    metadata = batch.get("metadata") if isinstance(batch.get("metadata"), dict) else {}
    return metadata.get("batching_strategy") == "stage3_spatial_tiles"


def _spatial_tile_batch_params(batch: dict[str, Any]) -> dict[str, Any]:
    metadata = batch.get("metadata") if isinstance(batch.get("metadata"), dict) else {}
    bbox = metadata.get("bbox_projected")
    projected_srid = metadata.get("projected_srid")
    stage3_grid_run_id = metadata.get("stage3_grid_run_id")
    stage3_grid_tile_id = metadata.get("stage3_grid_tile_id")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or projected_srid is None
        or stage3_grid_run_id is None
        or stage3_grid_tile_id is None
    ):
        raise RuntimeError(f"Stage 4 spatial batch metadata is incomplete: {batch['id']}")
    return {
        "tile_min_x": float(bbox[0]),
        "tile_min_y": float(bbox[1]),
        "tile_max_x": float(bbox[2]),
        "tile_max_y": float(bbox[3]),
        "tile_projected_srid": int(projected_srid),
        "stage3_grid_run_id": int(stage3_grid_run_id),
        "stage3_grid_tile_id": int(stage3_grid_tile_id),
    }


def _spatial_tile_batch_sql(batch: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if not _is_stage3_spatial_tile_batch(batch):
        return "", "", {}
    params = _spatial_tile_batch_params(batch)
    spatial_join_sql = """
        CROSS JOIN (
            SELECT
                ST_MakeEnvelope(
                    :tile_min_x,
                    :tile_min_y,
                    :tile_max_x,
                    :tile_max_y,
                    :tile_projected_srid
                ) AS geom_projected,
                ST_Transform(
                    ST_MakeEnvelope(
                        :tile_min_x,
                        :tile_min_y,
                        :tile_max_x,
                        :tile_max_y,
                        :tile_projected_srid
                    ),
                    4326
                ) AS geom_4326
        ) stage4_tile
        CROSS JOIN LATERAL (
            SELECT ST_PointOnSurface(ST_Transform(cell.geom, :tile_projected_srid)) AS point_projected
        ) stage4_cell
    """
    spatial_where_sql = """
        AND cell.geom && stage4_tile.geom_4326
        AND ST_X(stage4_cell.point_projected) >= :tile_min_x
        AND ST_X(stage4_cell.point_projected) < :tile_max_x
        AND ST_Y(stage4_cell.point_projected) >= :tile_min_y
        AND ST_Y(stage4_cell.point_projected) < :tile_max_y
    """
    return spatial_join_sql, spatial_where_sql, params


def _record_source_unavailable(
    run: dict[str, Any],
    batch: dict[str, Any],
    cells: list[dict[str, Any]],
    settings: Settings,
    error: str,
) -> Stage4BatchResult:
    from app.commands import fetch_cdse_ndvi

    start = datetime.combine(run["requested_start"], datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(run["requested_end"], datetime.max.time(), tzinfo=UTC)
    with _stage4_batch_session() as db:
        for cell in cells:
            fetch_cdse_ndvi.store_unavailable_observation(
                db,
                cell_id=int(cell["id"]),
                cell_uuid=str(cell["uuid"]),
                region_code=str(run["region_code"]),
                grid_version=str(run["grid_version"]),
                max_cloud_coverage=int(run["max_cloud_coverage"]),
                fallback_date=run["requested_end"],
                error=error,
                min_valid_pixel_ratio=float(run["min_valid_pixel_ratio"]),
                request_start=start,
                request_end=end,
                request_resolution=float(run["resolution"]),
                history_mode=False,
                pipeline_name=STAGE4_NDVI_PIPELINE_NAME,
            )
            mirror_stage4_observations_to_partitioned(
                db,
                grid_cell_id=int(cell["id"]),
                region_code=str(run["region_code"]),
                grid_version=str(run["grid_version"]),
            )
            sync_latest_ndvi_metric(db, run=run, batch=batch, cell=cell)
        upsert_alert(
            db,
            run_id=int(run["id"]),
            batch_id=int(batch["id"]),
            severity="critical",
            alert_type="cdse_unavailable",
            message="Stage 4 CDSE source is unavailable",
            details={"error": error},
        )
        record_event(
            db,
            run_id=int(run["id"]),
            batch_id=int(batch["id"]),
            severity="critical",
            event_type="cdse_unavailable",
            message="Stage 4 CDSE source is unavailable",
            details={"error": error},
        )
        db.commit()
    return Stage4BatchResult(
        processed_cells=len(cells),
        succeeded_cells=0,
        failed_cells=len(cells),
        skipped_cells=0,
        latest_error=error,
    )


def _record_catalogue_artifacts(
    run: dict[str, Any],
    batch: dict[str, Any],
    products: list[Any],
    settings: Settings,
) -> None:
    if not products:
        return
    with _stage4_batch_session() as db:
        for product in products:
            storage_uri = _artifact_uri(product, settings)
            db.execute(
                text(
                    """
                    INSERT INTO stage4_satellite_artifacts (
                        run_id,
                        batch_id,
                        region_id,
                        region_code,
                        grid_version,
                        provider,
                        artifact_type,
                        acquisition_date,
                        storage_uri,
                        metadata
                    )
                    VALUES (
                        :run_id,
                        :batch_id,
                        :region_id,
                        :region_code,
                        :grid_version,
                        'cdse',
                        'catalogue_product_reference',
                        :acquisition_date,
                        :storage_uri,
                        CAST(:metadata AS jsonb)
                    )
                    ON CONFLICT (
                        provider,
                        artifact_type,
                        acquisition_date,
                        storage_uri
                    ) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        batch_id = EXCLUDED.batch_id,
                        metadata = EXCLUDED.metadata
                    """
                ),
                {
                    "run_id": int(run["id"]),
                    "batch_id": int(batch["id"]),
                    "region_id": int(run["region_id"]),
                    "region_code": str(run["region_code"]),
                    "grid_version": str(run["grid_version"]),
                    "acquisition_date": product.acquisition_date,
                    "storage_uri": storage_uri,
                    "metadata": _json(
                        {
                            "product_id": product.product_id,
                            "name": product.name,
                            "cloud_coverage": product.cloud_coverage,
                            "processing_level": product.processing_level,
                            "history_storage_uri": settings.stage4_history_storage_uri or None,
                        }
                    ),
                },
            )
        db.commit()


def _record_satellite_artifact(
    db,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    provider: str,
    artifact_type: str,
    storage_uri: str,
    metadata: dict[str, Any],
) -> None:
    db.execute(
        text(
            """
            INSERT INTO stage4_satellite_artifacts (
                run_id,
                batch_id,
                region_id,
                region_code,
                grid_version,
                provider,
                artifact_type,
                acquisition_date,
                storage_uri,
                metadata
            )
            VALUES (
                :run_id,
                :batch_id,
                :region_id,
                :region_code,
                :grid_version,
                :provider,
                :artifact_type,
                :acquisition_date,
                :storage_uri,
                CAST(:metadata AS jsonb)
            )
            ON CONFLICT (
                provider,
                artifact_type,
                acquisition_date,
                storage_uri
            ) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                batch_id = EXCLUDED.batch_id,
                metadata = EXCLUDED.metadata
            """
        ),
        {
            "run_id": int(run["id"]),
            "batch_id": int(batch["id"]),
            "region_id": int(run["region_id"]),
            "region_code": str(run["region_code"]),
            "grid_version": str(run["grid_version"]),
            "provider": provider,
            "artifact_type": artifact_type,
            "acquisition_date": run["requested_end"],
            "storage_uri": storage_uri,
            "metadata": _json(metadata),
        },
    )


def _artifact_uri(product: Any, settings: Settings) -> str:
    if settings.stage4_history_storage_uri:
        base_uri = settings.stage4_history_storage_uri.rstrip("/")
        return f"{base_uri}/cdse/sentinel-2/{product.product_id}"
    return f"cdse://sentinel-2/{product.product_id}"


def ensure_public_raster_zonal_results(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Path:
    current_run_path = _public_raster_zonal_result_output_path(run, batch, settings)
    manifest_path = _public_raster_zonal_manifest_path(current_run_path)
    if current_run_path.is_file() and manifest_path.is_file():
        _validate_public_raster_zonal_manifest(current_run_path, run=run, batch=batch, settings=settings)
        return current_run_path
    if current_run_path.is_file() or manifest_path.is_file():
        # The manifest commits publication. An interrupted local pair is never
        # imported or adopted as a legacy CSV; rebuild it from a validated source.
        current_run_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        return _generate_public_raster_zonal_results(run, batch, settings)
    if settings.stage4_public_raster_zonal_reuse_existing_csv:
        try:
            existing_path = _public_raster_zonal_results_path(run, batch, settings)
        except RuntimeError as exc:
            if "result file is missing" not in str(exc):
                raise
        else:
            _validate_public_raster_zonal_manifest(existing_path, run=run, batch=batch, settings=settings)
            return existing_path
    return _generate_public_raster_zonal_results(run, batch, settings)


def _generate_public_raster_zonal_results(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Path:
    with _stage4_batch_session() as db:
        cells = _load_batch_cells(db, run, batch, include_geometry=True)
    if not cells:
        raise RuntimeError(f"Stage 4 public raster-zonal batch has no grid cells: {batch['id']}")

    result_path = _public_raster_zonal_result_output_path(run, batch, settings)
    work_dir = _public_raster_zonal_work_dir(run, batch, settings)
    work_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    bbox, crs_epsg = _public_raster_zonal_bbox(batch)
    source_bbox = _public_raster_zonal_source_bbox(bbox, settings)
    source_width, source_height = _public_raster_zonal_dimensions(source_bbox, settings)
    start = datetime.combine(run["requested_start"], datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(run["requested_end"], datetime.max.time(), tzinfo=UTC)
    raster_source = _ensure_public_raster_source_raster(
        run=run,
        batch=batch,
        settings=settings,
        bbox=source_bbox,
        crs_epsg=crs_epsg,
        width=source_width,
        height=source_height,
        start=start,
        end=end,
        work_dir=work_dir,
    )
    raster_path = raster_source["raster_path"]

    try:
        rows = _calculate_public_raster_zonal_rows(
            raster_path,
            cells=cells,
            run=run,
            batch=batch,
            settings=settings,
            bbox=bbox,
            crs_epsg=crs_epsg,
        )
        _write_public_raster_zonal_csv(result_path, rows)
        _write_public_raster_zonal_manifest(
            result_path,
            run=run,
            batch=batch,
            settings=settings,
            bbox=bbox,
            crs_epsg=crs_epsg,
            row_count=len(rows),
            source_raster=raster_source,
        )
        _record_public_raster_zonal_generation_artifacts(
            run=run,
            batch=batch,
            settings=settings,
            result_path=result_path,
            row_count=len(rows),
            source_raster=raster_source,
        )
    finally:
        if raster_source["temporary"]:
            raster_path.unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass
    return result_path


def _public_raster_source_identity(
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    stac = public_source_adapter_capabilities(settings).adapter == "public_stac_cog"
    return public_raster_cache_identity(
        run=run,
        batch=batch,
        settings=settings,
        bbox=bbox,
        crs_epsg=crs_epsg,
        width=width,
        height=height,
        target_mask_strategy="grid_cell_ids_in_source_tile_v2" if stac else "full_raster",
        target_mask_scope={
            "region_code": str(run["region_code"]),
            "grid_version": str(run["grid_version"]),
        } if stac else {},
        target_mask_sha256="grid-cell-mask-v2" if stac else None,
        target_min_valid_pixel_ratio=float(run["min_valid_pixel_ratio"]) if stac else None,
    )


def _ensure_public_raster_source_raster(
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
    start: datetime,
    end: datetime,
    work_dir: Path,
) -> dict[str, Any]:
    source_adapter = public_source_adapter_capabilities(settings)
    source_cells: list[dict[str, Any]] = []
    target_mask: dict[str, Any] | None = None
    identity = _public_raster_source_identity(
        run=run,
        batch=batch,
        settings=settings,
        bbox=bbox,
        crs_epsg=crs_epsg,
        width=width,
        height=height,
    )
    target_mask_strategy = identity.get("target_mask_strategy", "full_raster")
    target_mask_sha256 = identity.get("target_mask_sha256")
    cached_path = load_valid_public_raster_cache(settings, identity)
    if cached_path is not None:
        return _public_raster_zonal_cache_hit_result(
            cached_path,
            identity=identity,
            settings=settings,
            source_adapter=source_adapter.adapter,
            target_mask_strategy=target_mask_strategy,
            target_mask_sha256=target_mask_sha256,
        )
    with _public_raster_cache_generation_lock(settings, identity):
        cached_path = load_valid_public_raster_cache(settings, identity)
        if cached_path is not None:
            return _public_raster_zonal_cache_hit_result(
                cached_path,
                identity=identity,
                settings=settings,
                source_adapter=source_adapter.adapter,
                target_mask_strategy=target_mask_strategy,
                target_mask_sha256=target_mask_sha256,
            )

        source_metadata: dict[str, Any] = {}
        raster_bytes: bytes | None = None
        generated_path: Path | None = None
        if source_adapter.adapter == "cdse_process":
            from app.services.cdse import CdseClient

            client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
            token = client.get_access_token()
            raster_bytes = client.request_ndvi_raster(
                token,
                start=start,
                end=end,
                bbox=bbox,
                crs_epsg=crs_epsg,
                width=width,
                height=height,
                max_cloud_coverage=int(run["max_cloud_coverage"]),
            )
            source_metadata = {"request_family": source_adapter.request_family}
        elif source_adapter.adapter == "public_stac_cog":
            from app.services.stage4_public_cog import build_public_stac_ndvi_raster

            with _stage4_batch_session() as db:
                source_cells = _load_public_raster_source_tile_cells(
                    db,
                    run,
                    bbox=bbox,
                    crs_epsg=crs_epsg,
                )
            target_mask = _public_raster_zonal_target_mask(
                cells=source_cells,
                bbox=bbox,
                crs_epsg=crs_epsg,
                width=width,
                height=height,
                min_valid_pixel_ratio=float(run["min_valid_pixel_ratio"]),
            )
            generated_path = work_dir / "public-stac-cog-ndvi.tif"
            source_metadata = build_public_stac_ndvi_raster(
                settings=settings,
                bbox=bbox,
                crs_epsg=crs_epsg,
                width=width,
                height=height,
                start=start,
                end=end,
                max_cloud_coverage=int(run["max_cloud_coverage"]),
                output_path=generated_path,
                target_mask=target_mask["mask"],
                heartbeat=_stage4_batch_heartbeat_callback(int(batch["id"])),
            ) | {
                "source_target_cell_count": len(source_cells),
                "source_target_pixel_count": int(target_mask["target_pixel_count"]),
                "source_target_mask_sha256": str(target_mask["sha256"]),
                "source_tile_size_m": int(settings.stage4_public_raster_zonal_source_tile_size_m),
            }
            _validate_public_raster_zonal_tiff(generated_path)
        else:
            raise RuntimeError(
                f"Public source adapter {source_adapter.adapter} is not implemented for raster-zonal processing"
            )

        if settings.stage4_public_raster_cache_enabled:
            if generated_path is not None:
                raster_path = write_public_raster_cache_file(
                    settings,
                    identity,
                    generated_path,
                    _validate_public_raster_zonal_tiff,
                    source_metadata=source_metadata,
                )
                generated_path.unlink(missing_ok=True)
            else:
                if raster_bytes is None:
                    raise RuntimeError("Public raster source did not produce raster bytes")
                raster_path = write_public_raster_cache(
                    settings,
                    identity,
                    raster_bytes,
                    _validate_public_raster_zonal_tiff,
                    source_metadata=source_metadata,
                )
            return {
                "raster_path": raster_path,
                "identity": identity,
                "cache_key": public_raster_cache_key(identity),
                "cache_hit": False,
                "shared_cache": True,
                "temporary": False,
                "source_adapter": source_adapter.adapter,
                "source_metadata": source_metadata,
            }

        raster_path = work_dir / "ndvi.tif"
        if generated_path is not None:
            generated_path.replace(raster_path)
        else:
            if raster_bytes is None:
                raise RuntimeError("Public raster source did not produce raster bytes")
            raster_path.write_bytes(raster_bytes)
        _validate_public_raster_zonal_tiff(raster_path)
        return {
            "raster_path": raster_path,
            "identity": identity,
            "cache_key": public_raster_cache_key(identity),
            "cache_hit": False,
            "shared_cache": False,
            "temporary": True,
            "source_adapter": source_adapter.adapter,
            "source_metadata": source_metadata,
        }


def _public_raster_zonal_cache_hit_result(
    cached_path: Path,
    *,
    identity: dict[str, Any],
    settings: Settings,
    source_adapter: str,
    target_mask_strategy: str,
    target_mask_sha256: str | None,
) -> dict[str, Any]:
    _validate_public_raster_zonal_tiff(cached_path)
    source_metadata = read_public_raster_cache_source_metadata(settings, identity)
    source_metadata["cache_hit"] = True
    if source_adapter == "public_stac_cog":
        source_metadata.update(
            {
                "source_tile_size_m": int(settings.stage4_public_raster_zonal_source_tile_size_m),
                "target_mask_strategy": target_mask_strategy,
                "target_mask_identity": target_mask_sha256,
            }
        )
    return {
        "raster_path": cached_path,
        "identity": identity,
        "cache_key": public_raster_cache_key(identity),
        "cache_hit": True,
        "shared_cache": True,
        "temporary": False,
        "source_adapter": source_adapter,
        "source_metadata": source_metadata,
    }


def _record_public_raster_zonal_generation_artifacts(
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
    result_path: Path,
    row_count: int,
    source_raster: dict[str, Any],
) -> None:
    with _stage4_batch_session() as db:
        if source_raster["shared_cache"]:
            _record_satellite_artifact(
                db,
                run=run,
                batch=batch,
                provider=str(source_raster["source_adapter"]),
                artifact_type="source_raster_cache",
                storage_uri=str(source_raster["raster_path"]),
                metadata=public_raster_cache_metadata(
                    settings=settings,
                    identity=source_raster["identity"],
                    raster_path=source_raster["raster_path"],
                    cache_hit=bool(source_raster["cache_hit"]),
                )
                | {"source": source_raster.get("source_metadata") or {}},
            )
        _record_satellite_artifact(
            db,
            run=run,
            batch=batch,
            provider="public_raster_zonal",
            artifact_type="zonal_statistics_csv",
            storage_uri=str(result_path),
            metadata={
                "artifact_state": "zonal_done",
                "zonal_state": "zonal_done",
                "rows": int(row_count),
                "source_adapter": source_raster["source_adapter"],
                "source_cache_key": source_raster["cache_key"],
                "shared_cache": bool(source_raster["shared_cache"]),
                "source": source_raster.get("source_metadata") or {},
            },
        )
        db.commit()


def _public_raster_zonal_result_output_path(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Path:
    root = settings.stage4_public_raster_zonal_results_dir.strip()
    if not root:
        raise RuntimeError("STAGE4_PUBLIC_RASTER_ZONAL_RESULTS_DIR is required for public raster-zonal processing")
    return Path(root) / str(run["run_uuid"]) / f"batch-{int(batch['batch_index']):05d}.csv"


def _public_raster_zonal_work_dir(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Path:
    return (
        Path(settings.stage4_work_dir)
        / str(run["run_uuid"])
        / "public-raster-zonal"
        / f"batch-{int(batch['batch_index']):05d}"
    )


def _public_raster_zonal_manifest_path(result_path: Path) -> Path:
    return result_path.with_suffix(result_path.suffix + ".manifest.json")


def _public_raster_zonal_csv_sha256(result_path: Path) -> str:
    with result_path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_public_raster_zonal_manifest(
    result_path: Path,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
    bbox: list[float],
    crs_epsg: int,
    row_count: int,
    source_raster: dict[str, Any] | None = None,
) -> None:
    manifest_path = _public_raster_zonal_manifest_path(result_path)
    payload = {
        **_public_raster_zonal_manifest_identity(
            run=run,
            batch=batch,
            settings=settings,
            bbox=bbox,
            crs_epsg=crs_epsg,
        ),
        "row_count": int(row_count),
        "csv_sha256": _public_raster_zonal_csv_sha256(result_path),
    }
    if source_raster is not None:
        if (source_raster["cache_key"] != payload["source_cache_key"]
                or source_raster["source_adapter"] != payload["source_adapter"]):
            raise RuntimeError("Public raster-zonal source does not match manifest identity")
        payload.update(
            {
                "source_adapter": source_raster["source_adapter"],
                "source_cache_key": source_raster["cache_key"],
                "shared_cache": bool(source_raster["shared_cache"]),
                "cache_hit": bool(source_raster["cache_hit"]),
                "artifact_state": "zonal_done",
                "source": source_raster.get("source_metadata") or {},
            }
        )
    tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp_path.replace(manifest_path)


def _validate_public_raster_zonal_manifest(
    result_path: Path,
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> None:
    manifest_path = _public_raster_zonal_manifest_path(result_path)
    if not manifest_path.is_file():
        raise RuntimeError("Public raster-zonal result manifest is missing")
    payload = _read_public_raster_zonal_manifest_payload(result_path)
    bbox, crs_epsg = _public_raster_zonal_bbox(batch)
    expected = _public_raster_zonal_manifest_identity(
        run=run,
        batch=batch,
        settings=settings,
        bbox=bbox,
        crs_epsg=crs_epsg,
    )
    mismatches = [
        key
        for key, expected_value in expected.items()
        if payload.get(key) != expected_value
    ]
    if mismatches:
        raise RuntimeError(
            "Public raster-zonal result manifest does not match current run "
            f"({', '.join(mismatches)}); start a clean run with consistent source settings"
        )
    if int(payload.get("row_count") or 0) <= 0:
        raise RuntimeError("Public raster-zonal result manifest has no rows")
    if payload.get("csv_sha256") != _public_raster_zonal_csv_sha256(result_path):
        raise RuntimeError("Public raster-zonal result CSV checksum does not match manifest; start a clean run")
    if int(payload["row_count"]) != len(_read_public_raster_zonal_rows(result_path)):
        raise RuntimeError("Public raster-zonal result CSV row count does not match manifest; start a clean run")


def _read_public_raster_zonal_manifest_payload(result_path: Path) -> dict[str, Any]:
    manifest_path = _public_raster_zonal_manifest_path(result_path)
    if not manifest_path.is_file():
        return {}
    return json.loads(manifest_path.read_text())


def _public_raster_zonal_source_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    mosaic = source.get("mosaic") if isinstance(source.get("mosaic"), dict) else {}
    datetimes = [str(value) for value in source.get("selected_scene_datetimes") or [] if str(value).strip()]
    return {
        "source_adapter": manifest.get("source_adapter"),
        "source_cache_key": manifest.get("source_cache_key"),
        "shared_cache": bool(manifest.get("shared_cache")),
        "cache_hit": bool(manifest.get("cache_hit")),
        "scene_count": int(source.get("scene_count") or 0),
        "source_datetime_min": min(datetimes) if datetimes else None,
        "source_datetime_max": max(datetimes) if datetimes else None,
        "mosaic_scenes_attempted": int(mosaic.get("scenes_attempted") or 0),
        "mosaic_scenes_used": int(mosaic.get("scenes_used") or 0),
        "mosaic_skipped_scene_count": int(mosaic.get("skipped_scene_count") or 0),
        "target_coverage_complete": mosaic.get("target_coverage_complete"),
    }


def _public_raster_zonal_manifest_identity(
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
    bbox: list[float],
    crs_epsg: int,
) -> dict[str, Any]:
    source_bbox = _public_raster_zonal_source_bbox(bbox, settings)
    source_width, source_height = _public_raster_zonal_dimensions(source_bbox, settings)
    source_identity = _public_raster_source_identity(
        run=run, batch=batch, settings=settings, bbox=source_bbox,
        crs_epsg=crs_epsg, width=source_width, height=source_height,
    )
    return {
        "adapter": "public_raster_zonal",
        "source_adapter": public_source_adapter_capabilities(settings).adapter,
        **({"source_algorithm": PUBLIC_STAC_COG_SOURCE_ALGORITHM}
           if public_source_adapter_capabilities(settings).adapter == "public_stac_cog" else {}),
        "source_identity": source_identity,
        "source_cache_key": public_raster_cache_key(source_identity),
        "run_uuid": str(run["run_uuid"]),
        "region_code": str(run["region_code"]),
        "grid_version": str(run["grid_version"]),
        "batch_id": int(batch["id"]),
        "batch_index": int(batch["batch_index"]),
        "task_name": str(batch["task_name"]),
        "requested_start": str(run["requested_start"]),
        "requested_end": str(run["requested_end"]),
        "max_cloud_coverage": int(run["max_cloud_coverage"]),
        "min_valid_pixel_ratio": float(run["min_valid_pixel_ratio"]),
        "resolution_m": float(settings.stage4_public_raster_zonal_resolution_m),
        "bbox": [float(value) for value in bbox],
        "crs_epsg": int(crs_epsg),
    }


def _public_raster_zonal_bbox(batch: dict[str, Any]) -> tuple[list[float], int]:
    params = _spatial_tile_batch_params(batch)
    return (
        [
            float(params["tile_min_x"]),
            float(params["tile_min_y"]),
            float(params["tile_max_x"]),
            float(params["tile_max_y"]),
        ],
        int(params["tile_projected_srid"]),
    )


def _public_raster_zonal_dimensions(bbox: list[float], settings: Settings) -> tuple[int, int]:
    resolution_m = float(settings.stage4_public_raster_zonal_resolution_m)
    width = max(1, math.ceil((float(bbox[2]) - float(bbox[0])) / resolution_m))
    height = max(1, math.ceil((float(bbox[3]) - float(bbox[1])) / resolution_m))
    pixels = width * height
    if pixels > int(settings.stage4_public_raster_zonal_max_pixels):
        raise RuntimeError(
            f"Public raster-zonal tile needs {pixels} pixels; "
            f"configured limit is {settings.stage4_public_raster_zonal_max_pixels}"
        )
    return width, height


def _public_raster_zonal_source_bbox(bbox: list[float], settings: Settings) -> list[float]:
    source_tile_size = float(settings.stage4_public_raster_zonal_source_tile_size_m)
    batch_width = float(bbox[2]) - float(bbox[0])
    batch_height = float(bbox[3]) - float(bbox[1])
    if source_tile_size <= max(batch_width, batch_height):
        return [float(value) for value in bbox]
    min_x = math.floor(float(bbox[0]) / source_tile_size) * source_tile_size
    min_y = math.floor(float(bbox[1]) / source_tile_size) * source_tile_size
    return [
        float(min_x),
        float(min_y),
        float(min_x + source_tile_size),
        float(min_y + source_tile_size),
    ]


@contextmanager
def _public_raster_cache_generation_lock(settings: Settings, identity: dict[str, Any]):
    if not settings.stage4_public_raster_cache_enabled:
        yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover - production runs on Linux
        yield
        return
    lock_dir = Path(settings.stage4_public_raster_cache_dir) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{public_raster_cache_key(identity)}.lock"
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _validate_public_raster_zonal_tiff(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Public raster-zonal NDVI raster was not written")
    gdalinfo = shutil.which("gdalinfo")
    if not gdalinfo:
        return
    completed = subprocess.run(
        [gdalinfo, str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Public raster-zonal NDVI raster is not readable: {completed.stderr[-1000:]}")


def _load_public_raster_source_tile_cells(
    db,
    run: dict[str, Any],
    *,
    bbox: list[float],
    crs_epsg: int,
) -> list[dict[str, Any]]:
    table_name = _stage4_grid_cells_table(run)
    rows = db.execute(
        text(
            f"""
            SELECT
                cell.id,
                cell.uuid::text AS uuid,
                cell.area_ha,
                COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
                ST_AsGeoJSON(cell.geom)::json AS geometry,
                cell.grid_version
            FROM {table_name} cell
            CROSS JOIN (
                SELECT
                    ST_MakeEnvelope(
                        :tile_min_x,
                        :tile_min_y,
                        :tile_max_x,
                        :tile_max_y,
                        :tile_projected_srid
                    ) AS geom_projected,
                    ST_Transform(
                        ST_MakeEnvelope(
                            :tile_min_x,
                            :tile_min_y,
                            :tile_max_x,
                            :tile_max_y,
                            :tile_projected_srid
                        ),
                        4326
                    ) AS geom_4326
            ) source_tile
            CROSS JOIN LATERAL (
                SELECT ST_PointOnSurface(ST_Transform(cell.geom, :tile_projected_srid)) AS point_projected
            ) source_cell
            WHERE cell.region_id = :region_id
              AND cell.grid_version = :grid_version
              AND cell.geom && source_tile.geom_4326
              AND ST_X(source_cell.point_projected) >= :tile_min_x
              AND ST_X(source_cell.point_projected) < :tile_max_x
              AND ST_Y(source_cell.point_projected) >= :tile_min_y
              AND ST_Y(source_cell.point_projected) < :tile_max_y
            ORDER BY cell.id
            """
        ),
        {
            "region_id": int(run["region_id"]),
            "grid_version": str(run["grid_version"]),
            "tile_min_x": float(bbox[0]),
            "tile_min_y": float(bbox[1]),
            "tile_max_x": float(bbox[2]),
            "tile_max_y": float(bbox[3]),
            "tile_projected_srid": int(crs_epsg),
        },
    ).mappings().all()
    if not rows:
        raise RuntimeError("Public raster source tile has no grid cells")
    return [dict(row) for row in rows]


def _public_raster_zonal_target_mask(
    *,
    cells: list[dict[str, Any]],
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
    min_valid_pixel_ratio: float,
) -> dict[str, Any]:
    try:
        import numpy as np
        import rasterio
        from rasterio import features
        from rasterio.transform import from_bounds
        from rasterio.warp import transform_geom
    except ImportError as exc:  # pragma: no cover - checked by runtime readiness
        raise RuntimeError("Public raster-zonal processing requires rasterio") from exc

    transform = from_bounds(*bbox, width=int(width), height=int(height))
    shapes = [
        (
            transform_geom(
                "EPSG:4326",
                f"EPSG:{int(crs_epsg)}",
                _cell_geometry(cell),
                precision=6,
            ),
            index,
        )
        for index, cell in enumerate(cells, start=1)
    ]
    mask = features.rasterize(
        shapes,
        out_shape=(int(height), int(width)),
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=False,
    )
    sample_counts = [int(value) for value in np.bincount(mask.ravel(), minlength=len(cells) + 1)]
    required_counts = [
        int(math.ceil(value * float(min_valid_pixel_ratio))) if value > 0 else 0
        for value in sample_counts
    ]
    return {
        "mask": {
            "zones": mask,
            "sample_counts": sample_counts,
            "required_counts": required_counts,
            "min_valid_pixel_ratio": float(min_valid_pixel_ratio),
        },
        "sha256": hashlib.sha256(mask.tobytes()).hexdigest(),
        "target_pixel_count": int((mask > 0).sum()),
        "target_cell_count": len(cells),
        "min_valid_pixel_ratio": float(min_valid_pixel_ratio),
    }


def _calculate_public_raster_zonal_rows(
    raster_path: Path,
    *,
    cells: list[dict[str, Any]],
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
    bbox: list[float] | None = None,
    crs_epsg: int | None = None,
) -> list[dict[str, Any]]:
    try:
        import numpy as np
        import rasterio
        from rasterio import features
        from rasterio.warp import transform_geom
    except ImportError as exc:  # pragma: no cover - checked by runtime readiness
        raise RuntimeError("Public raster-zonal processing requires rasterio and numpy") from exc

    with rasterio.open(raster_path) as dataset:
        if dataset.count < 2:
            raise RuntimeError("Public raster-zonal NDVI raster must contain NDVI and valid-mask bands")
        window = None
        raster_transform = dataset.transform
        out_shape = (dataset.height, dataset.width)
        if bbox is not None and crs_epsg is not None and dataset.crs and dataset.crs.to_epsg() == int(crs_epsg):
            from rasterio.windows import Window, from_bounds

            metadata = batch.get("metadata") if isinstance(batch.get("metadata"), dict) else {}
            padding = max(
                float(settings.stage4_public_raster_zonal_resolution_m) * 2.0,
                float(metadata.get("grid_size_m") or 0),
            )
            window_bounds = (
                float(bbox[0]) - padding,
                float(bbox[1]) - padding,
                float(bbox[2]) + padding,
                float(bbox[3]) + padding,
            )
            raw_window = from_bounds(*window_bounds, transform=dataset.transform)
            window = raw_window.round_offsets().round_lengths()
            window = window.intersection(Window(0, 0, dataset.width, dataset.height))
            if int(window.width) <= 0 or int(window.height) <= 0:
                raise RuntimeError(f"Stage 4 public raster-zonal batch window is empty: {batch['id']}")
            raster_transform = dataset.window_transform(window)
            out_shape = (int(window.height), int(window.width))
        ndvi = dataset.read(1, window=window, masked=False)
        valid = dataset.read(2, window=window, masked=False)
        shapes = [
            (
                transform_geom(
                    "EPSG:4326",
                    dataset.crs,
                    _cell_geometry(cell),
                    precision=6,
                ),
                index,
            )
            for index, cell in enumerate(cells, start=1)
        ]
        zones = features.rasterize(
            shapes,
            out_shape=out_shape,
            transform=raster_transform,
            fill=0,
            dtype="int32",
            all_touched=False,
        )

    zone_flat = zones.ravel()
    inside = zone_flat > 0
    sample_counts = np.bincount(zone_flat[inside], minlength=len(cells) + 1)
    ndvi_flat = ndvi.ravel()
    valid_flat = valid.ravel()
    valid_pixels = (
        inside
        & (valid_flat > 0.5)
        & np.isfinite(ndvi_flat)
        & (ndvi_flat >= -1)
        & (ndvi_flat <= 1)
    )
    valid_counts = np.bincount(zone_flat[valid_pixels], minlength=len(cells) + 1)
    ndvi_sums = np.bincount(
        zone_flat[valid_pixels],
        weights=ndvi_flat[valid_pixels],
        minlength=len(cells) + 1,
    )

    metric_date = _parse_metric_date(run["requested_end"])
    source_adapter = public_source_adapter_capabilities(settings).adapter.replace("_", "-")
    source_bbox, source_crs = _public_raster_zonal_bbox(batch) if bbox is None or crs_epsg is None else (bbox, crs_epsg)
    source_identity = _public_raster_zonal_manifest_identity(
        run=run, batch=batch, settings=settings, bbox=source_bbox, crs_epsg=source_crs,
    )
    source_product_id = (
        f"{source_adapter}-{metric_date.isoformat()}-run-{run['run_uuid']}"
        f"-batch-{int(batch['batch_index']):05d}-source-{source_identity['source_cache_key']}"
    )
    rows = []
    for index, cell in enumerate(cells, start=1):
        sample_count = int(sample_counts[index]) if index < len(sample_counts) else 0
        valid_count = int(valid_counts[index]) if index < len(valid_counts) else 0
        valid_ratio = (valid_count / sample_count) if sample_count else 0.0
        mean_ndvi = (float(ndvi_sums[index]) / valid_count) if valid_count else None
        if sample_count <= 0:
            status = "no_sample_pixels"
        elif valid_count <= 0:
            status = "no_valid_pixels"
        elif valid_ratio < float(run["min_valid_pixel_ratio"]):
            status = "low_valid_pixel_ratio"
        else:
            status = "ok"
        rows.append(
            {
                "cell_id": int(cell["id"]),
                "metric_date": metric_date.isoformat(),
                "ndvi": f"{mean_ndvi:.6f}" if status == "ok" and mean_ndvi is not None else "",
                "source_status": status,
                "valid_pixel_ratio": f"{valid_ratio:.6f}",
                "sample_count": sample_count,
                "valid_pixel_count": valid_count,
                "source_product_id": source_product_id,
                "processing_level": "S2MSI2A",
                "cloud_coverage": "",
            }
        )
    return rows


def _cell_geometry(cell: dict[str, Any]) -> dict[str, Any]:
    geometry = cell.get("geometry")
    if isinstance(geometry, str):
        return json.loads(geometry)
    if isinstance(geometry, dict):
        return geometry
    raise RuntimeError(f"Stage 4 grid cell geometry is missing: {cell.get('id')}")


def _write_public_raster_zonal_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "cell_id",
        "metric_date",
        "ndvi",
        "source_status",
        "valid_pixel_ratio",
        "sample_count",
        "valid_pixel_count",
        "source_product_id",
        "processing_level",
        "cloud_coverage",
    ]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def _public_raster_zonal_results_path(
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: Settings,
) -> Path:
    root = settings.stage4_public_raster_zonal_results_dir.strip()
    if not root:
        raise RuntimeError("STAGE4_PUBLIC_RASTER_ZONAL_RESULTS_DIR is required for public raster-zonal imports")
    root_path = Path(root)
    batch_name = f"batch-{int(batch['batch_index']):05d}.csv"
    candidates = [
        root_path / str(run["run_uuid"]) / batch_name,
        root_path / str(run["run_uuid"]) / str(batch["task_name"]) / batch_name,
        root_path / batch_name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Public raster-zonal result file is missing for batch {batch['batch_index']}")


def _read_public_raster_zonal_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        if "cell_id" not in fieldnames:
            raise RuntimeError("Public raster-zonal result file must include cell_id")
        rows = []
        for row in reader:
            if not row.get("cell_id"):
                continue
            rows.append({key: value for key, value in row.items() if key is not None})
    return rows


def _batch_statistical_input_uri(run: dict[str, Any], batch: dict[str, Any], settings: Settings) -> str:
    root = settings.stage4_batch_statistical_input_uri or settings.stage4_history_storage_uri
    if not root:
        raise RuntimeError("STAGE4_BATCH_STATISTICAL_INPUT_URI is required")
    return "/".join(
        [
            root.rstrip("/"),
            _uri_part(str(run["region_code"])),
            _uri_part(str(run["grid_version"])),
            _batch_statistical_gpkg_name(batch),
        ]
    )


def _batch_statistical_output_uri(run: dict[str, Any], batch: dict[str, Any], settings: Settings) -> str:
    root = settings.stage4_batch_statistical_output_uri or settings.stage4_history_storage_uri
    if not root:
        raise RuntimeError("STAGE4_BATCH_STATISTICAL_OUTPUT_URI is required")
    return "/".join(
        [
            root.rstrip("/"),
            _uri_part(str(run["run_uuid"])),
            f"batch-{int(batch['batch_index']):05d}",
        ]
    )


def _batch_statistical_gpkg_name(batch: dict[str, Any]) -> str:
    return f"cells-{int(batch['grid_cell_id_start'])}-{int(batch['grid_cell_id_end'])}.gpkg"


def _batch_statistical_work_path(
    settings: Settings,
    run: dict[str, Any],
    batch: dict[str, Any],
    kind: str,
    filename: str | None = None,
) -> Path:
    parts = [
        Path(settings.stage4_work_dir),
        _uri_part(str(run["run_uuid"])),
        kind,
        f"batch-{int(batch['batch_index']):05d}",
    ]
    path = Path(*parts)
    return path / filename if filename else path


def _batch_statistical_statistics_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "data" in payload:
        return payload
    result = payload.get("result")
    if isinstance(result, dict) and "data" in result:
        return result
    return payload


def _batch_statistical_cell_id(payload: dict[str, Any]) -> int | None:
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    for candidate in (
        payload.get("identifier"),
        payload.get("id"),
        payload.get("featureId"),
        properties.get("identifier"),
    ):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _load_stage4_cell(db, cell_id: int, run: dict[str, Any]) -> dict[str, Any]:
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


def _uri_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _parse_metric_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text_value = str(value).strip()
    if "T" in text_value:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).date()
    return date.fromisoformat(text_value)


def _public_raster_zonal_status(row: dict[str, Any]) -> str:
    status = str(row.get("source_status") or row.get("status") or "").strip().lower()
    if status:
        return status
    return "ok" if str(row.get("ndvi") or "").strip() else "no_ndvi"


def _public_raster_zonal_ndvi(row: dict[str, Any], source_status: str) -> Decimal | None:
    raw_value = str(row.get("ndvi") or row.get("mean_ndvi") or "").strip()
    if not raw_value:
        if source_status == "ok":
            raise ValueError("Public raster-zonal ok row must include NDVI")
        return None
    value = Decimal(raw_value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    if value < Decimal("-1") or value > Decimal("1"):
        raise ValueError("NDVI must be between -1 and 1")
    if source_status != "ok":
        return None
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(float(value))


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        raise ValueError("chunk size must be greater than zero")
    return [items[index : index + size] for index in range(0, len(items), size)]


def _latest_metric(
    db,
    *,
    grid_cell_id: int,
    metric_name: str,
    region_code: str,
    grid_version: str,
) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT *
            FROM stage4_layer_b_metrics
            WHERE grid_cell_id = :grid_cell_id
              AND metric_name = :metric_name
              AND region_code = :region_code
              AND grid_version = :grid_version
            ORDER BY metric_date DESC, id DESC
            LIMIT 1
            """
        ),
        {
            "grid_cell_id": grid_cell_id,
            "metric_name": metric_name,
            "region_code": region_code,
            "grid_version": grid_version,
        },
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _heartbeat_batch(db, batch_id: int) -> None:
    db.execute(
        text(
            """
            UPDATE stage4_layer_b_batches
            SET heartbeat_at = now(),
                updated_at = now()
            WHERE id = :batch_id
            """
        ),
        {"batch_id": batch_id},
    )


def _stage4_batch_heartbeat_callback(batch_id: int):
    lease = _stage4_batch_lease.get()
    owner_thread = get_ident()

    def heartbeat() -> None:
        # Download threads do not inherit ContextVars and must never fall back
        # to an unleased SessionLocal or concurrently use the owner's connection.
        if get_ident() != owner_thread:
            return
        if lease is not None:
            if _stage4_batch_lease.get() is not lease:
                raise Stage4BatchLeaseLost("Stage 4 heartbeat outlived its batch lease")
            lease.assert_owned_connection()
        _heartbeat_batch_in_new_session(batch_id)

    return heartbeat


def _heartbeat_batch_in_new_session(batch_id: int) -> None:
    with _stage4_batch_session() as db:
        _heartbeat_batch(db, batch_id)
        db.commit()


def _plan_blockers(scope: Stage4Scope, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    capabilities = stage4_provider_capabilities(settings)
    blockers = []
    if scope.total_cells <= 0:
        blockers.append("Stage 4 scope has no grid cells")
    if scope.missing_area_coefficient_cells > 0:
        blockers.append(
            "area_coefficient backfill is required before full Layer B automation"
        )
    if capabilities.diagnostic_only and scope.total_cells > settings.stage4_public_statistics_hard_ceiling_cells:
        blockers.append(PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER)
    if (
        capabilities.adapter == "cdse_public_statistics"
        and scope.total_cells > settings.stage4_cell_statistics_max_cells
    ):
        blockers.append(
            "Public statistics proof must stay below the configured diagnostic cell limit"
        )
    if (
        capabilities.adapter == "cdse_public_statistics"
        and scope.total_cells > settings.stage4_public_statistics_max_cells_per_run
        and not settings.stage4_public_statistics_allow_full_scope
    ):
        blockers.append(
            "Public statistics path is limited to proof runs until full-scope scaling is explicitly enabled"
        )
    if capabilities.adapter == "cdse_public_statistics" and _has_industrial_batch_storage_config(settings):
        blockers.append(
            "Industrial Batch Statistical storage settings must stay empty while the public Stage 4 path is active"
        )
    if capabilities.adapter == "cdse_public_statistics" and _has_public_forbidden_s3_config(settings):
        blockers.append("S3 history storage must stay empty while the public Stage 4 path is active")
    if capabilities.adapter == "public_raster_zonal" and _has_industrial_batch_storage_config(settings):
        blockers.append(
            "Industrial Batch Statistical storage settings must stay empty while the public raster-zonal Stage 4 path is active"
        )
    if capabilities.adapter == "public_raster_zonal" and _has_public_forbidden_s3_config(settings):
        blockers.append("S3 history storage must stay empty while the public raster-zonal Stage 4 path is active")
    if capabilities.adapter == "public_raster_zonal":
        source_plan = public_source_plan(settings)
        source_adapter = public_source_adapter_capabilities(settings)
        if source_adapter.adapter == "cdse_process":
            blockers.append(
                "CDSE Process API source adapter is not allowed for client-approved public Stage 4; "
                "use the public STAC COG source adapter"
            )
        if (
            source_adapter.supports_shared_raster_cache
            and not settings.stage4_public_raster_cache_enabled
            and scope.total_cells > settings.stage4_public_statistics_hard_ceiling_cells
        ):
            blockers.append("Shared public raster cache is required before full-scope public raster-zonal runs")
        blockers.extend(
            blocker
            for blocker in source_plan["blockers"]
            if blocker != "Shared public raster cache is disabled"
        )
    if capabilities.adapter == "industrial_batch_statistical":
        input_uri = settings.stage4_batch_statistical_input_uri or settings.stage4_history_storage_uri
        output_uri = settings.stage4_batch_statistical_output_uri or settings.stage4_history_storage_uri
        if not input_uri or not output_uri:
            blockers.append(
                "Batch Statistical object storage URI is required for full-scope Stage 4 NDVI automation"
            )
        elif not _is_batch_statistical_s3_uri(input_uri) or not _is_batch_statistical_s3_uri(output_uri):
            blockers.append(
                "Batch Statistical input and output roots must be s3:// object-storage URIs"
            )
        elif not _has_batch_statistical_s3_transfer_access(settings):
            blockers.append(
                "Batch Statistical S3 access key pair is required for input upload and output import"
            )
    return blockers


def _has_industrial_batch_storage_config(settings: Settings) -> bool:
    return any(
        value.strip()
        for value in (
            settings.stage4_batch_statistical_input_uri,
            settings.stage4_batch_statistical_output_uri,
            settings.stage4_batch_statistical_s3_iam_role_arn,
            settings.stage4_batch_statistical_s3_access_key,
            settings.stage4_batch_statistical_s3_secret_access_key,
        )
    )


def _has_public_forbidden_s3_config(settings: Settings) -> bool:
    return settings.stage4_history_storage_uri.strip().lower().startswith("s3://")


def _is_batch_statistical_s3_uri(value: str) -> bool:
    return value.strip().lower().startswith("s3://")


def _has_batch_statistical_s3_transfer_access(settings: Settings) -> bool:
    access_key = settings.stage4_batch_statistical_s3_access_key.strip()
    bucket_key_pair = settings.stage4_batch_statistical_s3_secret_access_key.strip()
    return bool(access_key and bucket_key_pair)


def _ensure_grid_table(table_name: str) -> None:
    if table_name not in {"grid_cells", "grid_cells_partitioned"}:
        raise ValueError(f"Unsupported grid table: {table_name}")


def _table_exists(db, table_name: str) -> bool:
    if table_name not in {
        "grid_cells",
        "grid_cells_partitioned",
        "sentinel_observations_partitioned",
        "stage3_grid_generation_runs",
        "stage3_grid_generation_tiles",
    }:
        raise ValueError(f"Unsupported table: {table_name}")
    return bool(
        db.execute(
            text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _json(value: Any) -> str:
    return json.dumps(_redact_sensitive_metadata(value), default=_json_default, separators=(",", ":"))


def _redact_sensitive_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(
                marker in key_text
                for marker in (
                    "authorization",
                    "credential",
                    "secret",
                    "password",
                    "token",
                    "accesskey",
                    "access_key",
                    "secretaccesskey",
                    "signed_url",
                    "signature",
                )
            ):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_sensitive_metadata(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_metadata(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _decimal_to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _short_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:500]
