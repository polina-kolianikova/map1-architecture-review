import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.dashboard_summary_cache import cached_summary, refreshing_summary
from app.services.stage3_partitioned_reads import (
    grid_cells_table_sql,
    sentinel_observations_table_sql,
    verification_results_table_sql,
)
from app.services.source_strategy import stage1_source_strategy
from app.services.forest_scope import (
    COMPONENT_EXTENT_ROLE, component_extent_info, require_component_extent_publication,
)

router = APIRouter(prefix="/stage1", tags=["stage1"])

# Fixed native geometry budget before GeoJSON expansion, not a row-count limit.
REGION_GEOMETRY_MAX_BYTES = 1024 * 1024


def _current_observations_sql(
    sentinel_table: str, grid_table: str = "grid_cells", *, include_details: bool = True
) -> str:
    # Both pipelines may retain a current row. Count/list one latest row per
    # scoped cell, retaining explicit missing data alongside measured NDVI.
    columns = "observation.*" if include_details else """
        observation.grid_cell_id,
        observation.ndvi,
        COALESCE(
            observation.metadata->>'status',
            CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END
        ) AS observation_status
    """
    return f"""
        SELECT *
        FROM (
            SELECT
                {columns},
                row_number() OVER (
                    PARTITION BY observation.grid_cell_id
                    ORDER BY observation.acquisition_date DESC, observation.id DESC
                ) AS observation_rank
            FROM {sentinel_table} observation
            JOIN {grid_table} cell ON cell.id = observation.grid_cell_id
            WHERE observation.is_current
              AND observation.region_id = (SELECT id FROM regions WHERE code = :region_code)
              AND observation.region_code = :region_code
              AND observation.grid_version = :grid_version
              AND cell.region_id = observation.region_id
              AND cell.grid_version = :grid_version
              AND (observation.metadata->>'pipeline', observation.metadata->>'statistics_source') IN (
                  ('stage1_cdse_ndvi', 'sentinel_hub_statistical_api'),
                  ('stage4_layer_b_ndvi', 'sentinel_hub_statistical_api'),
                  ('stage4_layer_b_ndvi', 'public_raster_zonal')
              )
        ) ranked_observation
        WHERE observation_rank = 1
    """


def stage1_params(
    region_code: str | None = None,
    grid_version: str | None = None,
    db: Any | None = None,
) -> dict[str, object]:
    settings = get_settings()
    active_region_code = (region_code or settings.active_region_code).strip()
    active_grid_version = (grid_version or settings.active_grid_version).strip()
    region_source_layers = _region_forest_mask_source_layers(active_region_code, db=db)
    layer_a_source_layers = region_source_layers or settings.layer_a_source_layers
    forest_mask_source_layers = region_source_layers or settings.forest_mask_source_layers
    return {
        "region_code": active_region_code,
        "grid_version": active_grid_version,
        "layer_a_region_code": _layer_a_region_code(active_region_code, db=db),
        "layer_a_source_layers": layer_a_source_layers,
        "forest_mask_source_layers": forest_mask_source_layers,
    }


def _layer_a_region_code(region_code: str, db: Any | None = None) -> str:
    query = text(
        """
        SELECT COALESCE(NULLIF(region.metadata->'forest_mask'->>'source_region_code', ''), region.code)
        FROM regions region
        WHERE region.code = :region_code
        """
    )
    try:
        if db is not None:
            return db.execute(query, {"region_code": region_code}).scalar_one_or_none() or region_code
        with SessionLocal() as scoped_db:
            return scoped_db.execute(query, {"region_code": region_code}).scalar_one_or_none() or region_code
    except Exception:
        return region_code


def _region_forest_mask_source_layers(region_code: str, db: Any | None = None) -> list[str]:
    query = text(
        """
        SELECT COALESCE(region.metadata->'forest_mask'->'source_layers', '[]'::jsonb)
        FROM regions region
        WHERE region.code = :region_code
        """
    )
    try:
        if db is not None:
            payload = db.execute(query, {"region_code": region_code}).scalar_one_or_none()
        else:
            with SessionLocal() as scoped_db:
                payload = scoped_db.execute(query, {"region_code": region_code}).scalar_one_or_none()
    except Exception:
        return []
    if not payload:
        return []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, list):
        return []
    return [str(source).strip() for source in payload if str(source).strip()]


@router.get("/summary")
def get_summary(
    background_tasks: BackgroundTasks = None,
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    if background_tasks is not None:
        return cached_summary(
            region_code, grid_version, background_tasks,
            lambda: get_summary(region_code=region_code, grid_version=grid_version),
        )
    with SessionLocal() as db:
        if refreshing_summary():
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            db.execute(text("SET LOCAL statement_timeout = '600s'"))
            db.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
            db.execute(text("SET LOCAL work_mem = '64MB'"))
            db.execute(text("SET LOCAL enable_nestloop = off"))
            db.execute(text("SET LOCAL enable_seqscan = off"))
            db.execute(text("SET LOCAL random_page_cost = 1.1"))
            db.execute(text("SET LOCAL jit = off"))
            db.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
            db.execute(text("SET LOCAL client_connection_check_interval = '10s'"))
        params = stage1_params(region_code=region_code, grid_version=grid_version, db=db)
        grid_table = grid_cells_table_sql(params)
        sentinel_table = sentinel_observations_table_sql()
        verification_table = verification_results_table_sql()
        counts = db.execute(
            text(
                f"""
                WITH region_scope AS (
                    SELECT id
                    FROM regions
                    WHERE code = :region_code
                ),
                latest_completed_grid AS (
                    SELECT run.inserted_cells
                    FROM stage3_grid_generation_runs run
                    WHERE run.region_id = (SELECT id FROM region_scope)
                      AND run.grid_version = :grid_version
                      AND run.status = 'completed'
                      AND run.inserted_cells IS NOT NULL
                    ORDER BY run.finished_at DESC NULLS LAST, run.id DESC
                    LIMIT 1
                ),
                observation_counts AS (
                    {_summary_observation_counts_sql(sentinel_table, grid_table)}
                )
                SELECT
                    (
                        SELECT count(*)
                        FROM region_scope
                    ) AS regions,
                    COALESCE(
                        (SELECT inserted_cells FROM latest_completed_grid),
                        (
                            SELECT count(*)
                            FROM {grid_table} cell
                            WHERE cell.region_id = (SELECT id FROM region_scope)
                              AND cell.grid_version = :grid_version
                        )
                    ) AS grid_cells,
                    (
                        SELECT count(*)
                        FROM ogm_parcels
                        WHERE is_current
                          AND source_layer = ANY(:layer_a_source_layers)
                          AND attributes->>'region_code' = :layer_a_region_code
                    ) AS ogm_parcels,
                    (
                        SELECT total_count FROM observation_counts
                    ) AS sentinel_observations,
                    (
                        SELECT valid_count FROM observation_counts
                    ) AS real_sentinel_observations,
                    (
                        SELECT count(*)
                        FROM {verification_table} result
                        WHERE result.is_current
                          AND result.metadata->>'pipeline' = 'stage1_verification'
                          AND result.region_id = (SELECT id FROM region_scope)
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                    ) AS verification_results,
                    (
                        SELECT count(*)
                        FROM verification_audit_events event
                        WHERE event.metadata->>'pipeline' = 'stage1_verification_audit'
                          AND event.region_id = (SELECT id FROM region_scope)
                          AND event.region_code = :region_code
                          AND event.grid_version = :grid_version
                    ) AS verification_audit_events
                """
            ),
            params,
        ).mappings().one()
        if int(counts["regions"] or 0) == 0:
            raise HTTPException(status_code=404, detail="Region scope not found")
        if grid_version is not None and int(counts["grid_cells"] or 0) == 0:
            raise HTTPException(status_code=404, detail="Grid version not found for region scope")
        layer_a_sources = list(
            db.execute(
                text(
                    """
                    WITH configured_sources AS (
                        SELECT unnest(CAST(:layer_a_source_layers AS text[])) AS source_layer
                    ),
                    retained AS (
                        SELECT
                            source_layer,
                            attributes->'source_definition'->>'source_quality' AS source_quality,
                            official_status,
                            max((attributes->>'source_checked_at')::timestamptz)::text
                                AS last_successful_refresh_at,
                            max(last_seen_at)::text AS data_retained_at,
                            CASE
                                WHEN max((attributes->>'source_checked_at')::timestamptz) IS NULL THEN NULL
                                ELSE GREATEST(
                                    0,
                                    EXTRACT(
                                        DAY FROM (
                                            now() - max((attributes->>'source_checked_at')::timestamptz)
                                        )
                                    )::int
                                )
                            END AS data_age_days,
                            count(*) AS feature_count
                        FROM ogm_parcels
                        WHERE is_current
                          AND source_layer = ANY(:layer_a_source_layers)
                          AND attributes->>'region_code' = :layer_a_region_code
                        GROUP BY source_layer, source_quality, official_status
                    )
                    SELECT
                        CASE
                            WHEN configured_sources.source_layer = 'ogm_esatis_bolme' THEN 'OGM Bolme'
                            WHEN configured_sources.source_layer = 'ogm_yayin_mescere_wms_svg' THEN 'OGM Mescere'
                            ELSE 'Public Layer A source'
                        END AS source_name,
                        COALESCE(
                            retained.source_quality,
                            CASE
                                WHEN configured_sources.source_layer = 'ogm_esatis_bolme' THEN 'public_ogm_rest'
                                WHEN configured_sources.source_layer = 'ogm_yayin_mescere_wms_svg' THEN 'public_wms_derived'
                                ELSE 'configured_layer_a_source'
                            END
                        ) AS source_quality,
                        COALESCE(retained.official_status, 'configured_source') AS official_status,
                        retained.last_successful_refresh_at,
                        retained.data_retained_at,
                        latest_check.checked_at::text AS last_attempt_at,
                        COALESCE(latest_check.status, 'unknown') AS latest_check_status,
                        COALESCE(latest_check.feature_count, 0) AS latest_check_feature_count,
                        retained.data_age_days,
                        COALESCE(retained.feature_count, 0) AS feature_count
                    FROM configured_sources
                    LEFT JOIN retained ON retained.source_layer = configured_sources.source_layer
                    LEFT JOIN LATERAL (
                        SELECT checked_at, status, feature_count
                        FROM source_checks
                        WHERE source_checks.source_layer = configured_sources.source_layer
                          AND source_checks.region_code = :layer_a_region_code
                        ORDER BY checked_at DESC, id DESC
                        LIMIT 1
                    ) latest_check ON true
                    ORDER BY source_name, official_status
                    """
                ),
                params,
            ).mappings()
        )
        verification_status = list(
            db.execute(
                text(
                    f"""
                    WITH current_cells AS MATERIALIZED (
                        SELECT cell.id FROM {grid_table} cell
                        WHERE cell.region_id=(SELECT id FROM regions WHERE code=:region_code)
                          AND cell.grid_version=:grid_version
                    ),
                    current_results AS (
                        SELECT result.anomaly_type
                        FROM {verification_table} result
                        JOIN current_cells cell ON cell.id=result.grid_cell_id
                        WHERE result.is_current
                          AND result.metadata->>'pipeline' = 'stage1_verification'
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                          AND result.region_id=(SELECT id FROM regions WHERE code=:region_code)
                    ),
                    status_counts AS (
                        SELECT anomaly_type, count(*) AS result_count
                        FROM current_results
                        GROUP BY anomaly_type
                    )
                    SELECT anomaly_type, result_count
                    FROM status_counts
                    WHERE result_count > 0
                    UNION ALL
                    SELECT
                        'unverified' AS anomaly_type,
                        GREATEST(
                            CAST(:grid_cells AS bigint) - COALESCE((SELECT sum(result_count) FROM status_counts), 0),
                            0
                        ) AS result_count
                    WHERE GREATEST(
                        CAST(:grid_cells AS bigint) - COALESCE((SELECT sum(result_count) FROM status_counts), 0),
                        0
                    ) > 0
                    ORDER BY anomaly_type
                    """
                ),
                {**params, "grid_cells": int(counts["grid_cells"] or 0)},
            ).mappings()
        )
    summary = dict(counts)
    grid_count = summary.get("grid_cells") or 0
    real_observation_count = summary.get("real_sentinel_observations") or 0
    summary["region_code"] = params["region_code"]
    summary["grid_version"] = params["grid_version"]
    summary["region_exists"] = int(summary.get("regions") or 0) > 0
    summary["grid_exists"] = int(grid_count or 0) > 0
    summary["ndvi_coverage_ratio"] = (
        round(real_observation_count / grid_count, 5) if grid_count else 0
    )
    layer_a_source_rows = [_jsonable(row) for row in layer_a_sources]
    return {
        **summary,
        "layer_a_sources": layer_a_source_rows,
        "data_quality": _summary_data_quality(layer_a_source_rows),
        "verification_status": [dict(row) for row in verification_status],
    }


@router.get("/source-strategy")
def get_source_strategy():
    return stage1_source_strategy()


@router.get("/regions")
def list_regions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    """List the scoped region, preserving its identity even when geometry is large.

    `geometry` is the unchanged GeoJSON for native geometries up to 1 MiB
    (1,048,576 bytes measured by ST_MemSize), otherwise null. This fixed limit
    applies before GeoJSON conversion; it is not an HTTP response-size or total
    database-memory limit. No simplified or bounding-box forest is substituted.
    National previews use `/stage2/tiles/mask/{z}/{x}/{y}.mvt` and their existing
    preview bounds. Canonical stored geometries and calculations are unchanged.
    Opt-in component-authoritative extent storage always returns null geometry,
    an explicit geometry role and accepted forest area from native components.
    """
    with SessionLocal() as db:
        params = stage1_params(region_code=region_code, grid_version=grid_version, db=db)
        rows = list(
            db.execute(
                text(
                    """
                    SELECT
                        id,
                        name,
                        code,
                        country_code,
                        metadata,
                        CASE WHEN ST_MemSize(geom) <= :max_geometry_bytes
                             THEN ST_AsGeoJSON(geom, 17)::json
                             ELSE NULL::json
                        END AS geometry
                    FROM (
                        SELECT id, name, code, country_code, metadata,
                               CASE WHEN metadata->'region_geometry_role' IS NOT NULL
                                    THEN NULL ELSE geom END AS geom
                        FROM regions WHERE code = :region_code
                    ) regions
                    ORDER BY code
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset,
                 "max_geometry_bytes": REGION_GEOMETRY_MAX_BYTES},
            ).mappings()
        )
        results = []
        for row in rows:
            row = dict(row)
            metadata = row.pop("metadata", None)
            try:
                if component_extent_info(metadata) is not None:
                    publication = require_component_extent_publication(db, region_id=int(row["id"]), metadata=metadata)
                    row.update(geometry=None, region_geometry_role=COMPONENT_EXTENT_ROLE,
                               accepted_forest_area_ha=publication["accepted_area_ha"],
                               component_publication_id=publication["publication_id"])
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            results.append(row)
    return [_jsonable(row) for row in results]


@router.get("/layer-a")
def list_layer_a(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_geometry: bool = True,
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    if include_geometry:
        query = text(
            """
            SELECT
                parcel.id,
                CASE
                    WHEN parcel.source_layer = 'ogm_esatis_bolme' THEN 'OGM Bolme'
                    WHEN parcel.source_layer = 'ogm_yayin_mescere_wms_svg' THEN 'OGM Mescere'
                    ELSE 'Public Layer A source'
                END AS source_name,
                parcel.attributes->'source_definition'->>'source_quality' AS source_quality,
                parcel.forest_type,
                parcel.official_status,
                parcel.fetched_at,
                ST_AsGeoJSON(parcel.geom, 17)::json AS geometry
            FROM ogm_parcels parcel
            WHERE parcel.is_current
              AND parcel.source_layer = ANY(:layer_a_source_layers)
              AND parcel.attributes->>'region_code' = :layer_a_region_code
            ORDER BY parcel.source_layer, parcel.source_id NULLS LAST, parcel.id
            LIMIT :limit
            OFFSET :offset
            """
        )
    else:
        query = text(
            """
            SELECT
                parcel.id,
                CASE
                    WHEN parcel.source_layer = 'ogm_esatis_bolme' THEN 'OGM Bolme'
                    WHEN parcel.source_layer = 'ogm_yayin_mescere_wms_svg' THEN 'OGM Mescere'
                    ELSE 'Public Layer A source'
                END AS source_name,
                parcel.attributes->'source_definition'->>'source_quality' AS source_quality,
                parcel.forest_type,
                parcel.official_status,
                parcel.fetched_at,
                NULL AS geometry
            FROM ogm_parcels parcel
            WHERE parcel.is_current
              AND parcel.source_layer = ANY(:layer_a_source_layers)
              AND parcel.attributes->>'region_code' = :layer_a_region_code
            ORDER BY parcel.source_layer, parcel.source_id NULLS LAST, parcel.id
            LIMIT :limit
            OFFSET :offset
            """
        )

    with SessionLocal() as db:
        params = stage1_params(region_code=region_code, grid_version=grid_version, db=db)
        rows = list(
            db.execute(
                query,
                {**params, "limit": limit, "offset": offset},
            ).mappings()
        )
    return [_jsonable(row) for row in rows]


@router.get("/grid-cells")
def list_grid_cells(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    with SessionLocal() as db:
        params = stage1_params(region_code=region_code, grid_version=grid_version, db=db)
        grid_table = grid_cells_table_sql(params)
        rows = list(
            db.execute(
                text(
                    f"""
                    SELECT
                        cell.id,
                        cell.uuid::text AS uuid,
                        region.code AS region_code,
                        cell.area_ha,
                        cell.grid_version,
                        ST_AsGeoJSON(cell.geom, 17)::json AS geometry
                    FROM {grid_table} cell
                    JOIN regions region ON region.id = cell.region_id
                    WHERE cell.region_id = (
                          SELECT id
                          FROM regions
                          WHERE code = :region_code
                      )
                      AND cell.grid_version = :grid_version
                    ORDER BY cell.id
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset},
            ).mappings()
        )
    return [_jsonable(row) for row in rows]


@router.get("/observations")
def list_observations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = stage1_params(region_code=region_code, grid_version=grid_version)
    grid_table = grid_cells_table_sql(params)
    sentinel_table = sentinel_observations_table_sql()
    with SessionLocal() as db:
        rows = list(
            db.execute(
                text(
                    f"""
                    WITH current_observations AS (
                        {_current_observations_sql(sentinel_table, grid_table)}
                    )
                    SELECT
                        observation.id,
                        cell.uuid::text AS cell_uuid,
                        observation.acquisition_date,
                        observation.ndvi,
                        observation.cloud_coverage,
                        observation.processing_level,
                        COALESCE(
                            observation.metadata->>'status',
                            CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END
                        ) AS status,
                        observation.metadata->>'catalogue_match_type' AS catalogue_match_type,
                        observation.metadata->>'statistics_source' AS statistics_source,
                        CASE
                            WHEN observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                            THEN (observation.metadata->>'valid_pixel_count')::integer
                        END AS valid_pixel_count,
                        CASE
                            WHEN observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN (observation.metadata->>'valid_pixel_ratio')::numeric
                        END AS valid_pixel_ratio,
                        CASE
                            WHEN observation.metadata->>'sample_count' ~ '^[0-9]+$'
                            THEN (observation.metadata->>'sample_count')::integer
                        END AS sample_count
                    FROM current_observations observation
                    JOIN {grid_table} cell ON cell.id = observation.grid_cell_id
                    ORDER BY observation.acquisition_date, observation.id
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset},
            ).mappings()
        )
    return [_jsonable(row) for row in rows]


@router.get("/verification")
def list_verification(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = stage1_params(region_code=region_code, grid_version=grid_version)
    grid_table = grid_cells_table_sql(params)
    verification_table = verification_results_table_sql()
    with SessionLocal() as db:
        rows = list(
            db.execute(
                text(
                    f"""
                    WITH latest_checks AS (
                        SELECT DISTINCT ON (source_layer)
                            source_layer,
                            checked_at,
                            status,
                            feature_count
                        FROM source_checks
                        WHERE region_code = :layer_a_region_code
                        ORDER BY source_layer, checked_at DESC, id DESC
                    )
                    SELECT
                        result.id,
                        cell.uuid::text AS cell_uuid,
                        result.anomaly_type,
                        result.score,
                        result.explanation,
                        CASE
                            WHEN result.metadata->>'ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                            THEN (result.metadata->>'ndvi')::numeric
                        END AS ndvi,
                        result.metadata->>'acquisition_date' AS acquisition_date,
                        CASE
                            WHEN result.metadata->>'previous_ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                            THEN (result.metadata->>'previous_ndvi')::numeric
                        END AS previous_ndvi,
                        result.metadata->>'previous_acquisition_date' AS previous_acquisition_date,
                        CASE
                            WHEN result.metadata->>'previous_observation_id' ~ '^[0-9]+$'
                            THEN (result.metadata->>'previous_observation_id')::integer
                        END AS previous_observation_id,
                        result.metadata->>'layer_a_relation' AS layer_a_relation,
                        CASE
                            WHEN result.metadata->>'layer_a_intersection_area_ha' ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN (result.metadata->>'layer_a_intersection_area_ha')::numeric
                        END AS layer_a_intersection_area_ha,
                        CASE
                            WHEN result.metadata->>'layer_a_coverage_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN (result.metadata->>'layer_a_coverage_ratio')::numeric
                        END AS layer_a_coverage_ratio,
                        result.metadata->>'layer_a_source_name' AS layer_a_source_name,
                        result.metadata->>'layer_a_source_quality' AS layer_a_source_quality,
                        result.metadata->>'layer_a_official_status' AS layer_a_official_status,
                        result.metadata->>'layer_a_source_checked_at' AS layer_a_source_checked_at,
                        latest_check.checked_at::text AS layer_a_latest_check_at,
                        latest_check.status AS layer_a_latest_check_status,
                        latest_check.feature_count AS layer_a_latest_check_feature_count,
                        CASE
                            WHEN result.metadata->>'layer_a_source_checked_at' IS NULL
                              OR result.metadata->>'layer_a_source_checked_at' !~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                            THEN NULL
                            ELSE GREATEST(
                                0,
                                EXTRACT(
                                    DAY FROM (
                                        now() - (result.metadata->>'layer_a_source_checked_at')::timestamptz
                                    )
                                )::int
                            )
                        END AS layer_a_data_age_days,
                        COALESCE(
                            result.metadata->>'observation_status',
                            CASE WHEN result.metadata->>'ndvi' IS NOT NULL THEN 'ok' END
                        ) AS observation_status,
                        CASE
                            WHEN result.metadata->>'observation_cloud_coverage' ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN (result.metadata->>'observation_cloud_coverage')::numeric
                        END AS observation_cloud_coverage,
                        result.metadata->>'observation_statistics_source'
                            AS observation_statistics_source,
                        CASE
                            WHEN result.metadata->>'observation_valid_pixel_count' ~ '^[0-9]+$'
                            THEN (result.metadata->>'observation_valid_pixel_count')::integer
                        END AS observation_valid_pixel_count,
                        CASE
                            WHEN result.metadata->>'observation_valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN (result.metadata->>'observation_valid_pixel_ratio')::numeric
                        END AS observation_valid_pixel_ratio,
                        CASE
                            WHEN result.metadata->>'observation_sample_count' ~ '^[0-9]+$'
                            THEN (result.metadata->>'observation_sample_count')::integer
                        END AS observation_sample_count,
                        result.created_at
                    FROM {verification_table} result
                    JOIN {grid_table} cell ON cell.id = result.grid_cell_id
                        AND cell.region_id = result.region_id
                    JOIN regions region ON region.id = cell.region_id
                    LEFT JOIN latest_checks latest_check
                        ON latest_check.source_layer = result.metadata->>'ogm_source_layer'
                    WHERE result.is_current
                      AND result.metadata->>'pipeline' = 'stage1_verification'
                      AND result.region_code = :region_code
                      AND result.grid_version = :grid_version
                      AND region.code = :region_code
                      AND cell.grid_version = :grid_version
                    ORDER BY result.id
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset},
            ).mappings()
        )
    return [_jsonable(row) for row in rows]


@router.get("/verification-audit")
def list_verification_audit(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cell_uuid: UUID | None = None,
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage1_params(region_code=region_code, grid_version=grid_version),
        "limit": limit,
        "offset": offset,
        "cell_uuid": str(cell_uuid) if cell_uuid else None,
    }
    grid_table = grid_cells_table_sql(params)

    with SessionLocal() as db:
        rows = list(
            db.execute(
                text(
                    f"""
                    SELECT
                        event.id,
                        event.event_id::text AS event_id,
                        event.verification_result_id,
                        cell.uuid::text AS cell_uuid,
                        event.event_type,
                        event.previous_status,
                        event.new_status,
                        event.previous_score,
                        event.new_score,
                        event.reason,
                        event.actor_type,
                        event.created_at
                    FROM verification_audit_events event
                    JOIN {grid_table} cell ON cell.id = event.grid_cell_id
                        AND cell.region_id = event.region_id
                    JOIN regions region ON region.id = cell.region_id
                    WHERE event.metadata->>'pipeline' = 'stage1_verification_audit'
                      AND event.region_code = :region_code
                      AND event.grid_version = :grid_version
                      AND region.code = :region_code
                      AND cell.grid_version = :grid_version
                      AND (
                        CAST(:cell_uuid AS text) IS NULL
                        OR cell.uuid::text = CAST(:cell_uuid AS text)
                    )
                    ORDER BY event.created_at DESC, event.id DESC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                params,
            ).mappings()
        )
    return [_jsonable(row) for row in rows]


def _jsonable(row: Any) -> dict:
    data = dict(row)
    for key, value in list(data.items()):
        if isinstance(value, str) and value.startswith(("{", "[")):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return data


def _summary_data_quality(layer_a_sources: list[dict[str, Any]]) -> dict[str, Any]:
    if not layer_a_sources:
        return {
            "status": "degraded",
            "layer": "Layer A",
            "reason": "no_current_layer_a_sources",
            "message": "No current Layer A source rows are retained for the configured Stage 1 area.",
            "affected_sources": [],
        }

    retained_degraded = [
        source
        for source in layer_a_sources
        if source.get("latest_check_status") not in ("ok", "unknown")
        and int(source.get("feature_count") or 0) > 0
    ]
    if retained_degraded:
        oldest_age = max(
            int(source.get("data_age_days") or 0)
            for source in retained_degraded
        )
        statuses = sorted({str(source.get("latest_check_status")) for source in retained_degraded})
        return {
            "status": "degraded",
            "layer": "Layer A",
            "reason": "source_check_failed_retained_data",
            "message": (
                "Layer A is using retained public OGM data; latest source check "
                f"status is {', '.join(statuses)} and retained data is up to {oldest_age} days old."
            ),
            "affected_sources": [source.get("source_name") for source in retained_degraded],
        }

    missing_sources = [
        source
        for source in layer_a_sources
        if int(source.get("feature_count") or 0) == 0
    ]
    if missing_sources:
        return {
            "status": "degraded",
            "layer": "Layer A",
            "reason": "source_empty",
            "message": "One or more Layer A sources have no retained features.",
            "affected_sources": [source.get("source_name") for source in missing_sources],
        }

    return {
        "status": "ok",
        "layer": "Stage 1",
        "reason": "current_sources_available",
        "message": "Stage 1 public data sources are available.",
        "affected_sources": [],
    }


def _summary_observation_counts_sql(sentinel_table: str, grid_table: str) -> str:
    supported = """(observation.metadata->>'pipeline', observation.metadata->>'statistics_source') IN (
        ('stage1_cdse_ndvi', 'sentinel_hub_statistical_api'),
        ('stage4_layer_b_ndvi', 'sentinel_hub_statistical_api'),
        ('stage4_layer_b_ndvi', 'public_raster_zonal')
    )"""
    scope = f"""observation.is_current
        AND observation.region_id = (SELECT id FROM regions WHERE code = :region_code)
        AND observation.region_code = :region_code
        AND observation.grid_version = :grid_version
        AND {supported}"""
    return f"""
        WITH current_cells AS MATERIALIZED (
            SELECT cell.id
            FROM {grid_table} cell
            WHERE cell.region_id=(SELECT id FROM regions WHERE code=:region_code)
              AND cell.grid_version=:grid_version
        ),
        ranked AS (
            SELECT observation.id, observation.grid_cell_id, observation.region_id,
                row_number() OVER (
                    PARTITION BY observation.grid_cell_id
                    ORDER BY observation.acquisition_date DESC, observation.id DESC
                ) AS observation_rank
            FROM {sentinel_table} observation WHERE {scope}
        ),
        valid AS MATERIALIZED (
            SELECT DISTINCT observation.id
            FROM {sentinel_table} observation
            WHERE {scope} AND observation.ndvi IS NOT NULL
              AND COALESCE(observation.metadata->>'status',
                  CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
        )
        SELECT count(*) AS total_count, count(valid.id) AS valid_count
        FROM ranked
        JOIN current_cells cell ON cell.id=ranked.grid_cell_id
        LEFT JOIN valid ON valid.id=ranked.id
        WHERE ranked.observation_rank=1
    """
