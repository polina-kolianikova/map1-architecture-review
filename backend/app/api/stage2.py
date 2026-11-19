import gzip
import os
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import text

from app.api.stage1 import _jsonable, stage1_params
from app.db.session import SessionLocal
from app.services.stage2_tile_cache import TileCache, mask_revision, mask_tile_key
from app.services.carbon import carbon_api_defaults
from app.services.dates import valid_iso_date
from app.services.forest_scope import component_extent_info, require_component_extent_publication
from app.services.stage3_partitioned_reads import (
    grid_cells_table_sql,
    is_v10_public_scope,
    sentinel_observations_table_sql,
    use_partitioned_v10_grid_reads,
    verification_results_table_sql,
)
from app.services.stage5_carbon import (
    CARBON_ASSESSMENT_PIPELINES,
    CARBON_AUDIT_PIPELINES,
    STAGE5_CARBON_PIPELINE_NAME,
    load_stage5_config,
    stage5_annual_increment_query_params,
)

router = APIRouter(prefix="/stage2", tags=["stage2"])
MASK_TILE_CACHE = TileCache(published_dir=os.environ.get(
    "STAGE2_PUBLISHED_TILE_CACHE_DIR", "/app/artifacts/stage4/work/map-tiles/published-mask"
))
ASSET_STATUSES = {"all", "ok", "watch", "anomaly", "insufficient_data", "unverified"}
CARBON_STATUSES = {
    "all",
    "awaiting_coefficients",
    "baseline_estimate",
    "calculated",
    "draft_estimate",
    "failed",
    "pending_coefficients",
    "pending_inputs",
    "skipped_missing_evidence",
}
SIGNAL_STATUSES = {"all", "ok", "no_ndvi", "no_valid_pixels", "request_failed", "unknown"}
NDVI_PIPELINE_SQL = "('stage4_layer_b_ndvi', 'stage1_cdse_ndvi')"
NDVI_PIPELINE_PRIORITY_SQL = """
                            CASE
                                WHEN observation.metadata->>'pipeline' = 'stage4_layer_b_ndvi' THEN 0
                                ELSE 1
                            END
"""
CARBON_PIPELINE_SQL = "('" + "', '".join(CARBON_ASSESSMENT_PIPELINES) + "')"
CARBON_AUDIT_PIPELINE_SQL = "('" + "', '".join(CARBON_AUDIT_PIPELINES) + "')"
CARBON_PIPELINE_PRIORITY_SQL = f"""
                            CASE
                                WHEN assessment.metadata->>'pipeline' = '{STAGE5_CARBON_PIPELINE_NAME}' THEN 0
                                ELSE 1
                            END
"""


def _observation_status_sql(alias: str = "observation") -> str:
    # An edge cell without raster samples belongs to the existing empty-pixel
    # UI category. Keep the original provider reason in stored metadata.
    if alias not in {"observation", "result_observation"}:
        raise ValueError("Unsupported observation alias")
    return (
        f"CASE WHEN {alias}.metadata->>'status' = 'no_sample_pixels' "
        f"THEN 'no_valid_pixels' ELSE {alias}.metadata->>'status' END"
    )


def _carbon_api_params() -> dict[str, object]:
    return {"carbon_unit": "tCO2e"}


def _stage5_year_over_year_fallback_sql(carbon_alias: str = "latest_carbon") -> str:
    assessment_year = _stage5_assessment_year_sql(carbon_alias)
    return f"""
                    jsonb_build_object(
                        'enabled', true,
                        'comparison_source', 'stage4_layer_b_metrics',
                        'comparison_field', 'metric_date',
                        'delta_field', 'annual_ndvi_delta',
                        'current_year', {assessment_year},
                        'previous_assessment_id', {carbon_alias}.previous_annual_assessment_id,
                        'total_potential_delta_tco2e', CASE
                            WHEN {carbon_alias}.total_potential_delta_tco2e IS NULL THEN NULL
                            ELSE {carbon_alias}.total_potential_delta_tco2e::text
                        END,
                        'net_annual_increment_delta_tco2e_ha_yr', CASE
                            WHEN {carbon_alias}.net_annual_increment_delta_tco2e_ha_yr IS NULL THEN NULL
                            ELSE {carbon_alias}.net_annual_increment_delta_tco2e_ha_yr::text
                        END,
                        'net_annual_increment_delta_tco2e_yr', CASE
                            WHEN {carbon_alias}.net_annual_increment_delta_tco2e_yr IS NULL THEN NULL
                            ELSE {carbon_alias}.net_annual_increment_delta_tco2e_yr::text
                        END,
                        'comparison_state', COALESCE(
                            {carbon_alias}.source_snapshot->>'year_over_year_comparison_state',
                            CASE
                                WHEN {assessment_year} IS NULL THEN 'awaiting_current_year'
                                WHEN {carbon_alias}.previous_annual_assessment_id IS NULL
                                THEN 'awaiting_previous_carbon_snapshot'
                                ELSE 'annual_baseline'
                            END
                        )
                    )
    """


def _stage5_source_ndvi_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"""CASE
                        WHEN {carbon_alias}.source_snapshot->>'ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                        THEN ({carbon_alias}.source_snapshot->>'ndvi')::numeric
                    END"""


def _stage5_json_numeric_sql(expression: str) -> str:
    return f"""CASE
                        WHEN {expression} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                        THEN ({expression})::numeric
            END"""


def _stage5_current_annual_metadata_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"COALESCE(({carbon_alias}.metadata->'annual_increment' ? 'soil_increment'), false)"


def _stage5_assessment_year_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"(to_jsonb({carbon_alias})->>'assessment_year')::numeric"


def _stage5_gross_increment_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"(to_jsonb({carbon_alias})->>'gross_increment_tco2e_ha_yr')::numeric"


def _stage5_biomass_increment_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"(to_jsonb({carbon_alias})->>'biomass_increment_tco2e_ha_yr')::numeric"


def _stage5_soil_increment_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"(to_jsonb({carbon_alias})->>'soil_increment_tco2e_ha_yr')::numeric"


def _stage5_loss_factor_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"(to_jsonb({carbon_alias})->>'loss_factor_tco2e_ha_yr')::numeric"


def _stage5_net_annual_increment_ha_sql(carbon_alias: str = "latest_carbon") -> str:
    return f"(to_jsonb({carbon_alias})->>'net_annual_increment_tco2e_ha_yr')::numeric"


def _stage5_area_coefficient_sql(
    carbon_alias: str = "latest_carbon",
    cell_alias: str = "cell",
) -> str:
    return f"(to_jsonb({cell_alias})->>'area_coefficient')::numeric"


def _stage5_net_annual_increment_asset_sql(
    carbon_alias: str = "latest_carbon",
    cell_alias: str = "cell",
) -> str:
    return f"(to_jsonb({carbon_alias})->>'net_annual_increment_tco2e_yr')::numeric"


MIN_RAW_TILE_ZOOM = 12
MVT_GZIP_MIN_BYTES = 1024
MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM = 13
MAX_TILE_ZOOM = 16
MASK_TILE_PROJECTED_SRID = 6933
MASK_DETAIL_MIN_ZOOM = 10
MASK_MIN_COMPONENT_AREA_BY_ZOOM_HA = (
    (4, 500.0),
    (5, 100.0),
    (6, 50.0),
    (8, 10.0),
    (10, 1.0),
)
ASSET_PUBLIC_COLUMNS = """
            id,
            cell_uuid,
            region_code,
            area_ha,
            area_coefficient,
            grid_version,
            verification_result_id,
            anomaly_type,
            score,
            explanation,
            ndvi,
            acquisition_date,
            previous_ndvi,
            previous_acquisition_date,
            previous_observation_id,
            layer_a_relation,
            layer_a_intersection_area_ha,
            layer_a_coverage_ratio,
            layer_a_source_name,
            layer_a_source_quality,
            layer_a_official_status,
            layer_a_source_checked_at,
            layer_a_latest_check_at,
            layer_a_latest_check_status,
            layer_a_latest_check_feature_count,
            layer_a_data_age_days,
            observation_status,
            observation_cloud_coverage,
            observation_statistics_source,
            observation_valid_pixel_count,
            observation_valid_pixel_ratio,
            observation_sample_count,
            audit_event_count,
            latest_event_at,
            carbon_soil_tco2e,
            carbon_biomass_tco2e,
            carbon_root_tco2e,
            carbon_total_potential_tco2e,
            carbon_assessment_year,
            carbon_previous_annual_assessment_id,
            carbon_gross_increment_tco2e_ha_yr,
            carbon_loss_factor_tco2e_ha_yr,
            carbon_net_annual_increment_tco2e_ha_yr,
            carbon_net_annual_increment_tco2e_yr,
            carbon_total_potential_delta_tco2e,
            carbon_net_annual_increment_delta_tco2e_ha_yr,
            carbon_net_annual_increment_delta_tco2e_yr,
            carbon_unit,
            carbon_soil_input_status,
            carbon_biomass_input_status,
            carbon_root_input_status,
            carbon_total_potential_status,
            carbon_coefficient_status,
            carbon_coefficient_scope,
            carbon_methodology_version,
            carbon_calculation_status,
            carbon_coefficient_set_version,
            carbon_verification_factor,
            carbon_confidence_score,
            carbon_calibration_source,
            carbon_increment_model,
            carbon_audit_event_count,
            latest_carbon_event_at,
            carbon_readiness,
            created_at,
            geometry
"""


def _has_published_component_mask(db, params: dict[str, object]) -> bool:
    region = db.execute(
        text("SELECT id, metadata FROM regions WHERE code = :region_code"), params,
    ).mappings().one_or_none()
    if region is None:
        return False
    try:
        if component_extent_info(region["metadata"]) is None:
            return False
        require_component_extent_publication(
            db, region_id=int(region["id"]), metadata=region["metadata"],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return True


def _scope_has_current_asset_evidence(db, params: dict[str, object]) -> bool:
    sentinel_table = sentinel_observations_table_sql()
    verification_table = verification_results_table_sql()
    evidence = db.execute(
        text(f"""
            WITH region_scope AS (
                SELECT id FROM regions WHERE code = :region_code
            )
            SELECT EXISTS (
                SELECT 1 FROM {sentinel_table} observation
                WHERE observation.region_id = (SELECT id FROM region_scope)
                  AND observation.region_code = :region_code
                  AND observation.grid_version = :grid_version
                  AND observation.is_current
                  AND observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}
                LIMIT 1
            ) AS has_observations,
            EXISTS (
                SELECT 1 FROM {verification_table} result
                WHERE result.region_id = (SELECT id FROM region_scope)
                  AND result.region_code = :region_code
                  AND result.grid_version = :grid_version
                  AND result.is_current
                  AND result.metadata->>'pipeline' = 'stage1_verification'
                LIMIT 1
            ) AS has_verification,
            EXISTS (
                SELECT 1 FROM carbon_assessments assessment
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' IN {CARBON_PIPELINE_SQL}
                  AND EXISTS (
                      SELECT 1 FROM grid_cells cell
                      WHERE cell.id = assessment.grid_cell_id
                        AND cell.region_id = (SELECT id FROM region_scope)
                        AND cell.grid_version = :grid_version
                      -- Retain an evidence-driven correlated primary-key probe;
                      -- an empty carbon source must not become a grid scan.
                      OFFSET 0
                  )
                LIMIT 1
            ) AS has_carbon
        """),
        params,
    ).mappings().one()
    return any(evidence[key] for key in ("has_observations", "has_verification", "has_carbon"))


@router.get("/assets")
def list_assets(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str = Query("all"),
    carbon_status: str = Query("all"),
    signal_status: str = Query("all"),
    observed_from: str = Query(""),
    observed_to: str = Query(""),
    search: str = Query(""),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage1_params(region_code=region_code, grid_version=grid_version),
        **_carbon_api_params(),
        **_asset_filter_params(
            status=status,
            carbon_status=carbon_status,
            signal_status=signal_status,
            observed_from=observed_from,
            observed_to=observed_to,
            search=search,
        ),
        "limit": limit,
        "query_limit": limit + 1,
        "offset": offset,
    }
    partitioned_v10_reads = use_partitioned_v10_grid_reads(params)
    v10_public_scope = is_v10_public_scope(params)
    grid_table = grid_cells_table_sql(params) if partitioned_v10_reads else "grid_cells"
    with SessionLocal() as db:
        component_public_scope = not v10_public_scope and _has_published_component_mask(db, params)
        if component_public_scope:
            grid_table = "grid_cells"
        if (
            component_public_scope
            and _has_asset_filters(params)
            and not _asset_no_evidence_filters_match(params)
            and not _scope_has_current_asset_evidence(db, params)
        ):
            return {"limit": limit, "offset": offset, "total": 0,
                    "exact_total": True, "has_more": False, "rows": []}
        if not v10_public_scope and not component_public_scope:
            rows = list(db.execute(_asset_query("grid_cells"), params).mappings())
            count_query = (
                _asset_count_query("grid_cells")
                if _has_asset_filters(params)
                else _asset_scope_count_query("grid_cells")
            )
            total = db.execute(count_query, params).scalar_one()
            exact_total = True
            has_more = offset + limit < total
        else:
            has_filters = _has_asset_filters(params)
            if not has_filters:
                rows = list(db.execute(_asset_stage4_public_unfiltered_query(grid_table), params).mappings())
                total = db.execute(_asset_scope_total_query(grid_table), params).scalar_one()
                exact_total = True
                has_more = offset + limit < total
            else:
                if _asset_stage4_public_filters_match(params):
                    rows = list(db.execute(_asset_stage4_public_filtered_query(grid_table), params).mappings())
                    # A Stage 5 cache cannot prove the count of current evidence
                    # intersections. Bound the query and disclose a partial total.
                    total, exact_total, has_more = _paged_total(rows, limit=limit, offset=offset)
                else:
                    rows = []
                    total = 0
                    exact_total = True
                    has_more = False
    return {
        "limit": limit,
        "offset": offset,
        "total": total,
        "exact_total": exact_total,
        "has_more": has_more,
        "rows": [_jsonable(row) for row in rows[:limit]],
    }


@router.get("/assets/ids")
def list_asset_ids(
    limit: int = Query(10000, ge=1, le=20000),
    status: str = Query("all"),
    carbon_status: str = Query("all"),
    signal_status: str = Query("all"),
    observed_from: str = Query(""),
    observed_to: str = Query(""),
    search: str = Query(""),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage1_params(region_code=region_code, grid_version=grid_version),
        **_carbon_api_params(),
        **_asset_filter_params(
            status=status,
            carbon_status=carbon_status,
            signal_status=signal_status,
            observed_from=observed_from,
            observed_to=observed_to,
            search=search,
        ),
        "limit": limit,
    }
    partitioned_v10_reads = use_partitioned_v10_grid_reads(params)
    v10_public_scope = is_v10_public_scope(params)
    grid_table = grid_cells_table_sql(params) if partitioned_v10_reads else "grid_cells"
    with SessionLocal() as db:
        component_public_scope = not v10_public_scope and _has_published_component_mask(db, params)
        if component_public_scope:
            grid_table = "grid_cells"
        if (
            component_public_scope
            and _has_asset_filters(params)
            and not _asset_no_evidence_filters_match(params)
            and not _scope_has_current_asset_evidence(db, params)
        ):
            return {"limit": limit, "total": 0, "exact_total": True,
                    "truncated": False, "ids": []}
        if not v10_public_scope and not component_public_scope:
            rows = list(db.execute(_asset_ids_query("grid_cells"), params).mappings())
            count_query = (
                _asset_count_query("grid_cells")
                if _has_asset_filters(params)
                else _asset_scope_count_query("grid_cells")
            )
            total = db.execute(count_query, params).scalar_one()
        else:
            has_filters = _has_asset_filters(params)
            if has_filters:
                if _asset_stage4_public_filters_match(params):
                    id_params = {**params, "query_limit": limit + 1}
                    rows = list(db.execute(_asset_ids_stage4_public_filtered_query(grid_table), id_params).mappings())
                    total = len(rows)
                else:
                    rows = []
                    total = 0
            else:
                query = (
                    _asset_ids_unfiltered_query(grid_table)
                )
                rows = list(db.execute(query, params).mappings())
                total = db.execute(_asset_scope_total_query(grid_table), params).scalar_one()
    return {
        "limit": limit,
        "total": total,
        "exact_total": not ((v10_public_scope or component_public_scope) and has_filters and total > limit),
        "truncated": total > limit,
        "ids": [row["cell_uuid"] for row in rows[:limit]],
    }


@router.get("/assets/pick")
def pick_asset(
    lng: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    with SessionLocal() as db:
        params = {
            **stage1_params(region_code=region_code, grid_version=grid_version, db=db),
            "lng": lng,
            "lat": lat,
            "pick_tolerance_deg": 0.002,
        }
        grid_table = grid_cells_table_sql(params)
        row = db.execute(
            text(
                f"""
                WITH target_point AS (
                    SELECT ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS geom
                )
                SELECT
                    cell.id AS cell_id,
                    cell.uuid::text AS cell_uuid
                FROM {grid_table} cell
                JOIN regions region ON region.id = cell.region_id
                CROSS JOIN target_point point
                WHERE region.code = :region_code
                  AND cell.grid_version = :grid_version
                  AND cell.geom && ST_Expand(point.geom, :pick_tolerance_deg)
                  AND ST_DWithin(cell.geom, point.geom, :pick_tolerance_deg)
                ORDER BY
                    CASE WHEN ST_Intersects(cell.geom, point.geom) THEN 0 ELSE 1 END,
                    ST_Distance(cell.geom, point.geom),
                    cell.id
                LIMIT 1
                """
            ),
            params,
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Asset not found at map location")
    return _jsonable(row)


@router.get("/assets/{cell_uuid}")
def get_asset(
    cell_uuid: UUID,
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage1_params(region_code=region_code, grid_version=grid_version),
        **_carbon_api_params(),
        "cell_uuid": str(cell_uuid),
    }
    with SessionLocal() as db:
        if is_v10_public_scope(params):
            grid_table = grid_cells_table_sql(params)
            row = db.execute(
                _asset_stage4_public_detail_query(grid_table),
                params,
            ).mappings().one_or_none()
        else:
            row = db.execute(
                text(
                    f"""
                    {_asset_scope_sql("grid_cells", target_cell=True)}
                    SELECT {ASSET_PUBLIC_COLUMNS}
                    FROM assets
                    WHERE cell_uuid = CAST(:cell_uuid AS text)
                    LIMIT 1
                    """
                ),
                params,
            ).mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _jsonable(row)


@router.get("/assets/{cell_uuid}/ndvi-history")
def get_asset_ndvi_history(
    cell_uuid: UUID,
    months: int = Query(12, ge=1, le=36),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage1_params(region_code=region_code, grid_version=grid_version),
        "cell_uuid": str(cell_uuid),
        "months": months,
    }
    grid_table = grid_cells_table_sql(params)
    sentinel_table = sentinel_observations_table_sql()
    if is_v10_public_scope(params):
        history_query = _asset_stage4_public_history_query(grid_table, sentinel_table)
    else:
        ndvi_pipeline_filter = f"observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}"
        anchor_pipeline_filter = f"anchor.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}"
        pipeline_order_sql = f", {NDVI_PIPELINE_PRIORITY_SQL}"
        history_query = text(
            f"""
            SELECT
                daily.id,
                daily.grid_cell_id,
                daily.cell_uuid,
                daily.acquisition_date::text AS acquisition_date,
                daily.ndvi,
                daily.cloud_coverage,
                daily.status,
                daily.statistics_source,
                daily.valid_pixel_count,
                daily.valid_pixel_ratio
            FROM (
                SELECT DISTINCT ON (observation.acquisition_date)
                    observation.id,
                    observation.grid_cell_id,
                    cell.uuid::text AS cell_uuid,
                    observation.acquisition_date,
                    observation.ndvi,
                    observation.cloud_coverage,
                    COALESCE(
                        {_observation_status_sql("observation")},
                        CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END,
                        'unknown'
                    ) AS status,
                    observation.metadata->>'statistics_source' AS statistics_source,
                    CASE
                        WHEN observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                        THEN (observation.metadata->>'valid_pixel_count')::integer
                    END AS valid_pixel_count,
                    CASE
                        WHEN observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                        THEN (observation.metadata->>'valid_pixel_ratio')::numeric
                    END AS valid_pixel_ratio
                FROM {sentinel_table} observation
                JOIN {grid_table} cell ON cell.id = observation.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE cell.uuid = :cell_uuid
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
                  AND {ndvi_pipeline_filter}
                  AND observation.region_id = region.id
                  AND observation.region_code = :region_code
                  AND observation.grid_version = :grid_version
                  AND observation.acquisition_date >= (
                      SELECT COALESCE(max(anchor.acquisition_date), CURRENT_DATE)
                      FROM {sentinel_table} anchor
                      WHERE anchor.grid_cell_id = cell.id
                        AND {anchor_pipeline_filter}
                        AND anchor.region_id = region.id
                        AND anchor.region_code = :region_code
                        AND anchor.grid_version = :grid_version
                  ) - (:months * INTERVAL '1 month')
                ORDER BY
                    observation.acquisition_date
                    {pipeline_order_sql},
                    CASE
                        WHEN observation.metadata->>'status' = 'ok' THEN 0
                        WHEN observation.ndvi IS NOT NULL THEN 1
                        ELSE 2
                    END,
                    CASE
                        WHEN observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                        THEN (observation.metadata->>'valid_pixel_ratio')::numeric
                    END DESC NULLS LAST,
                    CASE
                        WHEN observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                        THEN (observation.metadata->>'valid_pixel_count')::integer
                    END DESC NULLS LAST,
                    observation.id DESC
            ) daily
            ORDER BY daily.acquisition_date
            LIMIT 500
            """
        )
    with SessionLocal() as db:
        rows = list(db.execute(history_query, params).mappings())
    if not rows:
        with SessionLocal() as db:
            exists = db.execute(
                text(
                    f"""
                    SELECT 1
                    FROM {grid_table} cell
                    JOIN regions region ON region.id = cell.region_id
                    WHERE cell.uuid = :cell_uuid
                      AND region.code = :region_code
                      AND cell.grid_version = :grid_version
                    LIMIT 1
                    """
                ),
                params,
            ).scalar_one_or_none()
        if not exists:
            raise HTTPException(status_code=404, detail="Asset not found")
    return {
        "cell_uuid": str(cell_uuid),
        "months": months,
        "rows": [_jsonable(row) for row in rows],
    }


@router.get("/tiles/grid/{z}/{x}/{y}.mvt")
def grid_tile(
    z: int,
    x: int,
    y: int,
    status: str = Query("all"),
    carbon_status: str = Query("all"),
    signal_status: str = Query("all"),
    observed_from: str = Query(""),
    observed_to: str = Query(""),
    search: str = Query(""),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    request: Request = None,
):
    _validate_tile(z, x, y)
    if z < MIN_RAW_TILE_ZOOM:
        return _mvt_response(None, request)
    params = _tile_params(
        z=z,
        x=x,
        y=y,
        status=status,
        carbon_status=carbon_status,
        signal_status=signal_status,
        observed_from=observed_from,
        observed_to=observed_to,
        search=search,
        region_code=region_code,
        grid_version=grid_version,
    )
    if is_v10_public_scope(params) and z < MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM:
        return _mvt_response(None, request)
    grid_table = grid_cells_table_sql(params)
    sentinel_table = sentinel_observations_table_sql()
    if is_v10_public_scope(params):
        with SessionLocal() as db:
            tile = db.execute(
                _stage4_public_tile_query(grid_table, sentinel_table, layer_name="grid"),
                params,
            ).scalar_one_or_none()
        return _mvt_response(tile, request)
    verification_table = verification_results_table_sql()
    with SessionLocal() as db:
        tile = db.execute(
            text(
                f"""
                WITH bounds AS (
                    SELECT
                        ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                        ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
                ),
                tile_cells AS (
                    SELECT
                        cell.id,
                        cell.uuid,
                        cell.area_ha,
                        cell.geom,
                        region.geom AS region_geom,
                        region.code AS region_code
                    FROM {grid_table} cell
                    JOIN regions region ON region.id = cell.region_id
                    CROSS JOIN bounds
                    WHERE region.code = :region_code
                      AND cell.grid_version = :grid_version
                      AND cell.geom && bounds.geom_4326
                      AND ST_Intersects(cell.geom, bounds.geom_4326)
                ),
                mvtgeom AS (
                    SELECT
                        cell.id,
                        cell.uuid::text AS uuid,
                        cell.area_ha,
                        cell.region_code,
                        {_asset_verification_status_sql("latest_result")} AS status,
                        latest_result.score,
                        COALESCE(
                            latest_observation.observation_status,
                            'unknown'
                        ) AS observation_status,
                        latest_observation.acquisition_date::text AS acquisition_date,
                        GREATEST(
                            COALESCE(latest_observation.ndvi, -1),
                            COALESCE(
                                CASE
                                    WHEN latest_result.metadata->>'previous_ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                                    THEN (latest_result.metadata->>'previous_ndvi')::numeric
                                END,
                                -1
                            )
                        ) AS forest_evidence_ndvi,
                        CASE
                            WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                            WHEN latest_carbon.status = 'calculated'
                              AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                                CASE
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                                    ELSE 'calculated'
                                END
                            ELSE latest_carbon.status
                        END AS carbon_total_potential_status,
                        COALESCE(latest_carbon.status, NULL::text)
                            AS carbon_calculation_status,
                        ST_AsMVTGeom(
                            ST_Transform(
                                ST_CollectionExtract(ST_MakeValid(cell.geom), 3),
                                3857
                            ),
                            bounds.geom_3857,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM tile_cells cell
                    CROSS JOIN bounds
                    LEFT JOIN LATERAL (
                        SELECT
                            result.anomaly_type,
                            result.score,
                            result.metadata
                        FROM {verification_table} result
                        WHERE result.grid_cell_id = cell.id
                          AND result.is_current
                          AND result.metadata->>'pipeline' = 'stage1_verification'
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                        ORDER BY result.created_at DESC, result.id DESC
                        LIMIT 1
                    ) latest_result ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            observation.acquisition_date,
                            observation.ndvi,
                            COALESCE(
                                {_observation_status_sql("observation")},
                                CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END,
                                'unknown'
                            ) AS observation_status
                        FROM {sentinel_table} observation
                        WHERE observation.grid_cell_id = cell.id
                          AND observation.is_current
                          AND observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}
                          AND observation.region_code = :region_code
                          AND observation.grid_version = :grid_version
                        ORDER BY observation.acquisition_date DESC, {NDVI_PIPELINE_PRIORITY_SQL}, observation.id DESC
                        LIMIT 1
                    ) latest_observation ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            assessment.id,
                            assessment.total_potential_tco2e,
                            assessment.status,
                            assessment.metadata
                        FROM carbon_assessments assessment
                        WHERE assessment.grid_cell_id = cell.id
                          AND assessment.is_current
                          AND assessment.metadata->>'pipeline' IN {CARBON_PIPELINE_SQL}
                        ORDER BY {CARBON_PIPELINE_PRIORITY_SQL}, assessment.created_at DESC, assessment.id DESC
                        LIMIT 1
                    ) latest_carbon ON true
                )
                SELECT ST_AsMVT(mvtgeom, 'grid', 4096, 'geom')
                FROM mvtgeom
                WHERE geom IS NOT NULL
                  AND (:status = 'all' OR status = :status)
                  AND (:carbon_status = 'all' OR carbon_total_potential_status = :carbon_status OR carbon_calculation_status = :carbon_status)
                  AND (:signal_status = 'all' OR observation_status = :signal_status)
                  AND (CAST(:observed_from AS date) IS NULL OR acquisition_date::date >= CAST(:observed_from AS date))
                  AND (CAST(:observed_to AS date) IS NULL OR acquisition_date::date <= CAST(:observed_to AS date))
                  AND (
                    :has_search IS false
                    OR lower(uuid) LIKE :search ESCAPE '\\'
                    OR lower(region_code) LIKE :search ESCAPE '\\'
                  )
                """
            ),
            params,
        ).scalar_one_or_none()
    return _mvt_response(tile, request)


@router.get("/tiles/mask/{z}/{x}/{y}.mvt")
def mask_tile(
    z: int,
    x: int,
    y: int,
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    request: Request = None,
):
    _validate_tile(z, x, y)
    mask_simplify_m = _mask_tile_simplify_m(z)
    with SessionLocal() as db:
        scope_params = (
            stage1_params(region_code=region_code, grid_version=grid_version, db=db)
            if region_code or grid_version
            else stage1_params(db=db)
        )
        mask_scope = db.execute(
            text(
                """
                SELECT
                    region.id AS region_id,
                    region.code AS region_code,
                    region.metadata AS region_metadata,
                    COALESCE(
                        NULLIF(region.metadata->'forest_mask'->>'mask_version', ''),
                        region.code
                    ) AS mask_version,
                    EXISTS (
                        SELECT 1
                        FROM stage3_region_mask_components component
                        WHERE component.region_id = region.id
                          AND component.mask_version = COALESCE(
                            NULLIF(region.metadata->'forest_mask'->>'mask_version', ''),
                            region.code
                          )
                          AND component.projected_srid = :mask_projected_srid
                        LIMIT 1
                    ) AS has_components
                FROM regions region
                WHERE region.code = :region_code
                """
            ),
            {**scope_params, "mask_projected_srid": MASK_TILE_PROJECTED_SRID},
        ).mappings().one_or_none()
        if mask_scope is None:
            return _mvt_response(None, request)
        authoritative_components = False
        try:
            if component_extent_info(mask_scope.get("region_metadata")) is not None:
                if not mask_scope["has_components"]:
                    raise RuntimeError("Processing extent authoritative components missing")
                require_component_extent_publication(
                    db, region_id=int(mask_scope["region_id"]), metadata=mask_scope["region_metadata"],
                    mask_version=mask_scope["mask_version"], projected_srid=MASK_TILE_PROJECTED_SRID,
                )
                authoritative_components = True
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        mask_geom_expr = (
            "ST_Transform({geom_column}, 3857)"
            if mask_simplify_m <= 0
            else "ST_SimplifyPreserveTopology(ST_Transform({geom_column}, 3857), :mask_simplify_m)"
        )
        params = {
            **scope_params,
            "z": z,
            "x": x,
            "y": y,
            "region_id": int(mask_scope["region_id"]),
            "mask_version": mask_scope["mask_version"],
            "mask_projected_srid": MASK_TILE_PROJECTED_SRID,
            "mask_detail_min_zoom": MASK_DETAIL_MIN_ZOOM,
            "mask_simplify_m": mask_simplify_m,
            "mask_min_component_area_ha": _mask_tile_min_component_area_ha(z),
        }
        if mask_scope["has_components"]:
            component_geom_expr = mask_geom_expr.format(geom_column="component.geom_4326")
            # The published global 1 ha rule applies to connected components;
            # storage fragments may each be smaller, including at overview zooms.
            fragment_area_filter = (
                "" if authoritative_components
                else "AND component.area_ha >= :mask_min_component_area_ha"
            )
            tile_sql = text(
                f"""
                WITH bounds AS (
                    SELECT
                        ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                        ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
                ),
                mvtgeom AS (
                    SELECT
                        component.id,
                        CAST(:region_code AS text) AS region_code,
                        ST_AsMVTGeom(
                            {component_geom_expr},
                            bounds.geom_3857,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM stage3_region_mask_components component
                    CROSS JOIN bounds
                    WHERE component.region_id = :region_id
                      AND component.mask_version = :mask_version
                      AND component.projected_srid = :mask_projected_srid
                      {fragment_area_filter}
                      AND component.geom_4326 && bounds.geom_4326
                      AND ST_Intersects(component.geom_4326, bounds.geom_4326)
                )
                SELECT ST_AsMVT(mvtgeom, 'mask', 4096, 'geom')
                FROM mvtgeom
                WHERE geom IS NOT NULL
                """
            )
        else:
            region_geom_expr = mask_geom_expr.format(geom_column="region.geom")
            tile_sql = text(
                f"""
                WITH bounds AS (
                    SELECT
                        ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                        ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
                ),
                mvtgeom AS (
                    SELECT
                        0::bigint AS id,
                        region.code AS region_code,
                        ST_AsMVTGeom(
                            {region_geom_expr},
                            bounds.geom_3857,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM regions region
                    CROSS JOIN bounds
                    WHERE region.id = :region_id
                      AND region.geom && bounds.geom_4326
                      AND ST_Intersects(region.geom, bounds.geom_4326)
                )
                SELECT ST_AsMVT(mvtgeom, 'mask', 4096, 'geom')
                FROM mvtgeom
                WHERE geom IS NOT NULL
                """
            )
    cacheable = authoritative_components and request is not None and z <= 12
    revision = mask_revision(mask_scope["region_metadata"]) if cacheable else None

    def load_tile():
        with SessionLocal() as tile_db:
            if cacheable:
                tile_db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                current_metadata = tile_db.execute(text(
                    "SELECT metadata FROM regions WHERE id = :region_id"
                ), params).scalar_one()
                current_revision = mask_revision(current_metadata)
                if current_revision != revision:
                    raise HTTPException(status_code=409, detail="Forest mask changed; retry the tile")
                require_component_extent_publication(
                    tile_db, region_id=params["region_id"], metadata=current_metadata,
                    mask_version=params["mask_version"], projected_srid=MASK_TILE_PROJECTED_SRID,
                )
            return tile_db.execute(tile_sql, params).scalar_one_or_none()

    if cacheable:
        key = mask_tile_key(revision, params["region_id"], params["mask_version"],
                            params["grid_version"], MASK_TILE_PROJECTED_SRID, z, x, y)
        try:
            tile = MASK_TILE_CACHE.get_or_load(key, load_tile)
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "2"}) from exc
    else:
        tile = load_tile()
    return _mvt_response(tile, request)


@router.get("/tiles/layer-a/{z}/{x}/{y}.mvt")
def layer_a_tile(
    z: int,
    x: int,
    y: int,
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    request: Request = None,
):
    _validate_tile(z, x, y)
    with SessionLocal() as db:
        scope_params = (
            stage1_params(region_code=region_code, grid_version=grid_version, db=db)
            if region_code or grid_version
            else stage1_params(db=db)
        )
        params = {
            **scope_params,
            "z": z,
            "x": x,
            "y": y,
        }
        tile = db.execute(
            text(
                """
                WITH bounds AS (
                    SELECT
                        ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                        ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
                ),
                mvtgeom AS (
                    SELECT
                        parcel.id,
                        CASE
                            WHEN parcel.source_layer = 'ogm_esatis_bolme' THEN 'OGM Bolme'
                            WHEN parcel.source_layer = 'ogm_yayin_mescere_wms_svg' THEN 'OGM Mescere'
                            ELSE 'Public Layer A source'
                        END AS source_name,
                        parcel.forest_type,
                        parcel.official_status,
                        parcel.attributes->'source_definition'->>'source_quality' AS source_quality,
                        ST_AsMVTGeom(
                            ST_Transform(parcel.geom, 3857),
                            bounds.geom_3857,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM ogm_parcels parcel
                    CROSS JOIN bounds
                    WHERE parcel.is_current
                      AND parcel.source_layer = ANY(:layer_a_source_layers)
                      AND parcel.attributes->>'region_code' = :layer_a_region_code
                      AND parcel.geom && bounds.geom_4326
                      AND ST_Intersects(parcel.geom, bounds.geom_4326)
                )
                SELECT ST_AsMVT(mvtgeom, 'layer_a', 4096, 'geom')
                FROM mvtgeom
                WHERE geom IS NOT NULL
                """
            ),
            params,
        ).scalar_one_or_none()
    return _mvt_response(tile, request)


@router.get("/tiles/layer-b/{z}/{x}/{y}.mvt")
def layer_b_tile(
    z: int,
    x: int,
    y: int,
    status: str = Query("all"),
    carbon_status: str = Query("all"),
    signal_status: str = Query("all"),
    observed_from: str = Query(""),
    observed_to: str = Query(""),
    search: str = Query(""),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    request: Request = None,
):
    _validate_tile(z, x, y)
    if z < MIN_RAW_TILE_ZOOM:
        return _mvt_response(None, request)
    params = _tile_params(
        z=z,
        x=x,
        y=y,
        status=status,
        carbon_status=carbon_status,
        signal_status=signal_status,
        observed_from=observed_from,
        observed_to=observed_to,
        search=search,
        region_code=region_code,
        grid_version=grid_version,
    )
    if is_v10_public_scope(params) and z < MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM:
        return _mvt_response(None, request)
    grid_table = grid_cells_table_sql(params)
    sentinel_table = sentinel_observations_table_sql()
    if is_v10_public_scope(params):
        with SessionLocal() as db:
            tile = db.execute(
                _stage4_public_tile_query(grid_table, sentinel_table, layer_name="layer_b"),
                params,
            ).scalar_one_or_none()
        return _mvt_response(tile, request)
    verification_table = verification_results_table_sql()
    with SessionLocal() as db:
        tile = db.execute(
            text(
                f"""
                WITH bounds AS (
                    SELECT
                        ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                        ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
                ),
                tile_cells AS (
                    SELECT
                        cell.id,
                        cell.uuid,
                        cell.geom,
                        region.geom AS region_geom,
                        region.code AS region_code
                    FROM {grid_table} cell
                    JOIN regions region ON region.id = cell.region_id
                    CROSS JOIN bounds
                    WHERE region.code = :region_code
                      AND cell.grid_version = :grid_version
                      AND cell.geom && bounds.geom_4326
                      AND ST_Intersects(cell.geom, bounds.geom_4326)
                ),
                mvtgeom AS (
                    SELECT
                        cell.id,
                        cell.uuid::text AS uuid,
                        cell.region_code,
                        {_asset_verification_status_sql("latest_result")}
                            AS verification_status,
                        latest_observation.ndvi,
                        latest_observation.cloud_coverage,
                        latest_observation.status,
                        latest_observation.statistics_source,
                        latest_observation.acquisition_date::text AS acquisition_date,
                        GREATEST(
                            COALESCE(latest_observation.ndvi, -1),
                            COALESCE(
                                CASE
                                    WHEN latest_result.metadata->>'previous_ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                                    THEN (latest_result.metadata->>'previous_ndvi')::numeric
                                END,
                                -1
                            )
                        ) AS forest_evidence_ndvi,
                        CASE
                            WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                            WHEN latest_carbon.status = 'calculated'
                              AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                                CASE
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                                    ELSE 'calculated'
                                END
                            ELSE latest_carbon.status
                        END AS carbon_total_potential_status,
                        COALESCE(latest_carbon.status, NULL::text)
                            AS carbon_calculation_status,
                        ST_AsMVTGeom(
                            ST_Transform(
                                ST_CollectionExtract(ST_MakeValid(cell.geom), 3),
                                3857
                            ),
                            bounds.geom_3857,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM tile_cells cell
                    CROSS JOIN bounds
                    LEFT JOIN LATERAL (
                        SELECT
                            observation.acquisition_date,
                            observation.ndvi,
                            observation.cloud_coverage,
                            COALESCE(
                                {_observation_status_sql("observation")},
                                CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END
                            ) AS status,
                            observation.metadata->>'statistics_source' AS statistics_source
                        FROM {sentinel_table} observation
                        WHERE observation.grid_cell_id = cell.id
                          AND observation.is_current
                          AND observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}
                          AND observation.region_code = :region_code
                          AND observation.grid_version = :grid_version
                        ORDER BY observation.acquisition_date DESC, {NDVI_PIPELINE_PRIORITY_SQL}, observation.id DESC
                        LIMIT 1
                    ) latest_observation ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            result.anomaly_type,
                            result.metadata
                        FROM {verification_table} result
                        WHERE result.grid_cell_id = cell.id
                          AND result.is_current
                          AND result.metadata->>'pipeline' = 'stage1_verification'
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                        ORDER BY result.created_at DESC, result.id DESC
                        LIMIT 1
                    ) latest_result ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            assessment.id,
                            assessment.total_potential_tco2e,
                            assessment.status,
                            assessment.metadata
                        FROM carbon_assessments assessment
                        WHERE assessment.grid_cell_id = cell.id
                          AND assessment.is_current
                          AND assessment.metadata->>'pipeline' IN {CARBON_PIPELINE_SQL}
                        ORDER BY {CARBON_PIPELINE_PRIORITY_SQL}, assessment.created_at DESC, assessment.id DESC
                        LIMIT 1
                    ) latest_carbon ON true
                )
                SELECT ST_AsMVT(mvtgeom, 'layer_b', 4096, 'geom')
                FROM mvtgeom
                WHERE geom IS NOT NULL
                  AND (:status = 'all' OR verification_status = :status)
                  AND (:carbon_status = 'all' OR carbon_total_potential_status = :carbon_status OR carbon_calculation_status = :carbon_status)
                  AND (:signal_status = 'all' OR status = :signal_status)
                  AND (CAST(:observed_from AS date) IS NULL OR acquisition_date::date >= CAST(:observed_from AS date))
                  AND (CAST(:observed_to AS date) IS NULL OR acquisition_date::date <= CAST(:observed_to AS date))
                  AND (
                    :has_search IS false
                    OR lower(uuid) LIKE :search ESCAPE '\\'
                    OR lower(region_code) LIKE :search ESCAPE '\\'
                  )
                """
            ),
            params,
        ).scalar_one_or_none()
    return _mvt_response(tile, request)


@router.get("/tiles/layer-c/{z}/{x}/{y}.mvt")
def layer_c_tile(
    z: int,
    x: int,
    y: int,
    status: str = Query("all"),
    carbon_status: str = Query("all"),
    signal_status: str = Query("all"),
    observed_from: str = Query(""),
    observed_to: str = Query(""),
    search: str = Query(""),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    request: Request = None,
):
    _validate_tile(z, x, y)
    if z < MIN_RAW_TILE_ZOOM:
        return _mvt_response(None, request)
    params = _tile_params(
        z=z,
        x=x,
        y=y,
        status=status,
        carbon_status=carbon_status,
        signal_status=signal_status,
        observed_from=observed_from,
        observed_to=observed_to,
        search=search,
        region_code=region_code,
        grid_version=grid_version,
    )
    if is_v10_public_scope(params) and z < MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM:
        return _mvt_response(None, request)
    grid_table = grid_cells_table_sql(params)
    sentinel_table = sentinel_observations_table_sql()
    verification_table = verification_results_table_sql()
    status_sql = _asset_verification_status_sql("latest_result")
    with SessionLocal() as db:
        tile = db.execute(
            text(
                f"""
                WITH bounds AS (
                    SELECT
                        ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                        ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
                ),
                tile_cells AS (
                    SELECT
                        cell.id,
                        cell.region_id,
                        cell.uuid,
                        cell.geom,
                        region.geom AS region_geom,
                        region.code AS region_code
                    FROM {grid_table} cell
                    JOIN regions region ON region.id = cell.region_id
                    CROSS JOIN bounds
                    WHERE region.code = :region_code
                      AND cell.grid_version = :grid_version
                      AND cell.geom && bounds.geom_4326
                      AND ST_Intersects(cell.geom, bounds.geom_4326)
                ),
                mvtgeom AS (
                    SELECT
                        cell.id,
                        cell.uuid::text AS uuid,
                        cell.region_code,
                        latest_result.id AS verification_result_id,
                        {status_sql} AS status,
                        latest_result.score,
                        latest_result.acquisition_date,
                        latest_result.layer_a_relation,
                        COALESCE(
                            latest_observation.observation_status,
                            'unknown'
                        ) AS observation_status,
                        latest_observation.acquisition_date::text AS observation_acquisition_date,
                        GREATEST(
                            COALESCE(latest_observation.ndvi, -1),
                            COALESCE(
                                CASE
                                    WHEN latest_result.metadata->>'previous_ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                                    THEN (latest_result.metadata->>'previous_ndvi')::numeric
                                END,
                                -1
                            )
                        ) AS forest_evidence_ndvi,
                        CASE
                            WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                            WHEN latest_carbon.status = 'calculated'
                              AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                                CASE
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                                    ELSE 'calculated'
                                END
                            ELSE latest_carbon.status
                        END AS carbon_total_potential_status,
                        COALESCE(latest_carbon.status, NULL::text)
                            AS carbon_calculation_status,
                        ST_AsMVTGeom(
                            ST_Transform(
                                ST_CollectionExtract(ST_MakeValid(cell.geom), 3),
                                3857
                            ),
                            bounds.geom_3857,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM tile_cells cell
                    CROSS JOIN bounds
                    LEFT JOIN LATERAL (
                        SELECT
                            result.id,
                            result.anomaly_type,
                            result.score,
                            result.metadata->>'acquisition_date' AS acquisition_date,
                            result.metadata->>'layer_a_relation' AS layer_a_relation,
                            result.metadata
                        FROM {verification_table} result
                        WHERE result.grid_cell_id = cell.id
                          AND result.region_id = cell.region_id
                          AND result.is_current
                          AND result.metadata->>'pipeline' = 'stage1_verification'
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                        ORDER BY result.created_at DESC, result.id DESC
                        LIMIT 1
                    ) latest_result ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            observation.acquisition_date,
                            observation.ndvi,
                            COALESCE(
                                {_observation_status_sql("observation")},
                                CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END,
                                'unknown'
                            ) AS observation_status
                        FROM {sentinel_table} observation
                        WHERE observation.grid_cell_id = cell.id
                          AND observation.region_id = cell.region_id
                          AND observation.is_current
                          AND observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}
                          AND observation.region_code = :region_code
                          AND observation.grid_version = :grid_version
                        ORDER BY observation.acquisition_date DESC, {NDVI_PIPELINE_PRIORITY_SQL}, observation.id DESC
                        LIMIT 1
                    ) latest_observation ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            assessment.id,
                            assessment.total_potential_tco2e,
                            assessment.status,
                            assessment.metadata
                        FROM carbon_assessments assessment
                        WHERE assessment.grid_cell_id = cell.id
                          AND assessment.is_current
                          AND assessment.metadata->>'pipeline' IN {CARBON_PIPELINE_SQL}
                        ORDER BY {CARBON_PIPELINE_PRIORITY_SQL}, assessment.created_at DESC, assessment.id DESC
                        LIMIT 1
                    ) latest_carbon ON true
                )
                SELECT ST_AsMVT(mvtgeom, 'layer_c', 4096, 'geom')
                FROM mvtgeom
                WHERE geom IS NOT NULL
                  AND (:status = 'all' OR status = :status)
                  AND (:carbon_status = 'all' OR carbon_total_potential_status = :carbon_status OR carbon_calculation_status = :carbon_status)
                  AND (:signal_status = 'all' OR observation_status = :signal_status)
                  AND (CAST(:observed_from AS date) IS NULL OR observation_acquisition_date::date >= CAST(:observed_from AS date))
                  AND (CAST(:observed_to AS date) IS NULL OR observation_acquisition_date::date <= CAST(:observed_to AS date))
                  AND (
                    :has_search IS false
                    OR (CAST(:search_cell_id AS bigint) IS NOT NULL AND id = CAST(:search_cell_id AS bigint))
                    OR (CAST(:search_uuid_exact AS text) IS NOT NULL AND uuid = CAST(:search_uuid_exact AS text))
                    OR lower(replace(uuid, '-', '')) LIKE :search ESCAPE '\\'
                    OR lower(uuid) LIKE :search ESCAPE '\\'
                    OR lower(region_code) LIKE :search ESCAPE '\\'
                  )
                """
            ),
            params,
        ).scalar_one_or_none()
    return _mvt_response(tile, request)


def _asset_query(grid_table: str):
    return text(
        f"""
        {_asset_scope_sql(grid_table)}
        SELECT {ASSET_PUBLIC_COLUMNS}
        FROM assets
        WHERE (:status = 'all' OR anomaly_type = :status)
          AND (:carbon_status = 'all' OR carbon_total_potential_status = :carbon_status OR carbon_calculation_status = :carbon_status)
          AND (:signal_status = 'all' OR observation_status = :signal_status)
          AND (CAST(:observed_from AS date) IS NULL OR acquisition_date::date >= CAST(:observed_from AS date))
          AND (CAST(:observed_to AS date) IS NULL OR acquisition_date::date <= CAST(:observed_to AS date))
          AND (
            :has_search IS false
            OR lower(cell_uuid) LIKE :search ESCAPE '\\'
            OR lower(region_code) LIKE :search ESCAPE '\\'
          )
        ORDER BY id
        LIMIT :query_limit
        OFFSET :offset
        """
    )


def _asset_unfiltered_query(grid_table: str):
    return _asset_no_evidence_grid_query(grid_table, extra_where="")


def _asset_no_evidence_query(grid_table: str):
    return _asset_no_evidence_grid_query(
        grid_table,
        extra_where=f"AND {_asset_no_evidence_where_sql()}",
    )


def _asset_no_evidence_grid_query(grid_table: str, *, extra_where: str):
    return text(
        f"""
        SELECT
            cell.id,
            cell.uuid::text AS cell_uuid,
            CAST(:region_code AS text) AS region_code,
            cell.area_ha,
            COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
            cell.grid_version,
            NULL::bigint AS verification_result_id,
            'unverified'::text AS anomaly_type,
            NULL::numeric AS score,
            'No verification result recorded for this asset yet.'::text AS explanation,
            NULL::numeric AS ndvi,
            NULL::text AS acquisition_date,
            NULL::numeric AS previous_ndvi,
            NULL::text AS previous_acquisition_date,
            NULL::integer AS previous_observation_id,
            NULL::text AS layer_a_relation,
            NULL::numeric AS layer_a_intersection_area_ha,
            NULL::numeric AS layer_a_coverage_ratio,
            NULL::text AS layer_a_source_name,
            NULL::text AS layer_a_source_quality,
            NULL::text AS layer_a_official_status,
            NULL::text AS layer_a_source_checked_at,
            NULL::text AS layer_a_latest_check_at,
            NULL::text AS layer_a_latest_check_status,
            NULL::integer AS layer_a_latest_check_feature_count,
            NULL::integer AS layer_a_data_age_days,
            'unknown'::text AS observation_status,
            NULL::numeric AS observation_cloud_coverage,
            NULL::text AS observation_statistics_source,
            NULL::integer AS observation_valid_pixel_count,
            NULL::numeric AS observation_valid_pixel_ratio,
            NULL::integer AS observation_sample_count,
            0::bigint AS audit_event_count,
            NULL::text AS latest_event_at,
            NULL::numeric AS carbon_soil_tco2e,
            NULL::numeric AS carbon_biomass_tco2e,
            NULL::numeric AS carbon_root_tco2e,
            NULL::numeric AS carbon_total_potential_tco2e,
            NULL::integer AS carbon_assessment_year,
            NULL::bigint AS carbon_previous_annual_assessment_id,
            NULL::numeric AS carbon_gross_increment_tco2e_ha_yr,
            NULL::numeric AS carbon_loss_factor_tco2e_ha_yr,
            NULL::numeric AS carbon_net_annual_increment_tco2e_ha_yr,
            NULL::numeric AS carbon_net_annual_increment_tco2e_yr,
            NULL::numeric AS carbon_total_potential_delta_tco2e,
            NULL::numeric AS carbon_net_annual_increment_delta_tco2e_ha_yr,
            NULL::numeric AS carbon_net_annual_increment_delta_tco2e_yr,
            CAST(:carbon_unit AS text) AS carbon_unit,
            'required'::text AS carbon_soil_input_status,
            'required'::text AS carbon_biomass_input_status,
            'required'::text AS carbon_root_input_status,
            'awaiting_coefficients'::text AS carbon_total_potential_status,
            CAST(NULL::text AS text) AS carbon_coefficient_status,
            NULL::text AS carbon_coefficient_scope,
            CAST(NULL::text AS text) AS carbon_methodology_version,
            CAST(NULL::text AS text) AS carbon_calculation_status,
            NULL::text AS carbon_coefficient_set_version,
            NULL::numeric AS carbon_verification_factor,
            NULL::numeric AS carbon_confidence_score,
            NULL::text AS carbon_calibration_source,
            NULL::text AS carbon_increment_model,
            0::bigint AS carbon_audit_event_count,
            NULL::text AS latest_carbon_event_at,
            jsonb_build_object(
                'methodology_version', CAST(NULL::text AS text),
                'soil', jsonb_build_object('value', NULL, 'unit', CAST(:carbon_unit AS text), 'input_status', 'required'),
                'biomass', jsonb_build_object('value', NULL, 'unit', CAST(:carbon_unit AS text), 'input_status', 'required'),
                'root_system', jsonb_build_object('value', NULL, 'unit', CAST(:carbon_unit AS text), 'input_status', 'required'),
                'total_potential', jsonb_build_object('value', NULL, 'unit', CAST(:carbon_unit AS text), 'status', 'awaiting_coefficients'),
                'annual_increment', jsonb_build_object(
                    'increment_model', 'baseline-ipcc-ogm-v0.1',
                    'unit', 'tCO2e/ha/year',
                    'asset_unit', 'tCO2e/year',
                    'input_unit', 'tC/ha/year',
                    'stoichiometric_factor', CAST(NULL::numeric AS numeric)::text,
                    'gross_increment', jsonb_build_object('value', NULL, 'unit', 'tCO2e/ha/year'),
                    'biomass_increment', jsonb_build_object('value', NULL, 'unit', 'tCO2e/ha/year'),
                    'soil_increment', jsonb_build_object('value', NULL, 'unit', 'tCO2e/ha/year'),
                    'loss_factor', jsonb_build_object('value', NULL, 'unit', 'tCO2e/ha/year'),
                    'net_per_ha_year', jsonb_build_object('value', NULL, 'unit', 'tCO2e/ha/year', 'status', 'awaiting_coefficients'),
                    'net_per_asset_year', jsonb_build_object('value', NULL, 'unit', 'tCO2e/year', 'status', 'awaiting_coefficients'),
                    'net_delta_per_ha_year', jsonb_build_object('value', NULL, 'unit', 'tCO2e/ha/year', 'status', 'awaiting_previous_carbon_snapshot'),
                    'net_delta_per_asset_year', jsonb_build_object('value', NULL, 'unit', 'tCO2e/year', 'status', 'awaiting_previous_carbon_snapshot')
                ),
                'verification_factor', NULL,
                'verification_factor_status', CAST(NULL::text AS text),
                'confidence_score', NULL,
                'calibration_source', NULL,
                'layer_d_status', CAST(NULL::text AS text),
                'audit_event_count', 0,
                'latest_event_at', NULL,
                'evidence_state', 'awaiting_verification',
                'methodology_state', CAST(NULL::text AS text),
                'calculation_state', CAST(NULL::text AS text),
                'last_calculated_at', NULL,
                'trend_interval_days', NULL,
                'year_over_year', jsonb_build_object(
                    'enabled', true,
                    'comparison_source', 'stage4_layer_b_metrics',
                    'comparison_field', 'metric_date',
                    'delta_field', 'annual_ndvi_delta',
                    'comparison_state', 'awaiting_evidence'
                ),
                'coefficient_set_version', NULL,
                'coefficient_scope', NULL,
                'coefficient_status', CAST(NULL::text AS text),
                'calculation_status', CAST(NULL::text AS text)
            ) AS carbon_readiness,
            NULL::timestamptz AS created_at,
            NULL::json AS geometry
        FROM {grid_table} cell
        WHERE cell.region_id = (
              SELECT id
              FROM regions
              WHERE code = :region_code
          )
          AND cell.grid_version = :grid_version
          {extra_where}
        ORDER BY cell.region_id, cell.grid_version, cell.id
        LIMIT :query_limit
        OFFSET :offset
        """
    )


def _stage4_public_tile_query(grid_table: str, sentinel_table: str, *, layer_name: str):
    if layer_name == "grid":
        layer_columns = """
                        joined.id,
                        joined.uuid,
                        joined.area_ha,
                        joined.region_code,
                        joined.status,
                        joined.score,
                        joined.observation_status,
                        joined.observation_acquisition_date::text AS acquisition_date,
                        joined.forest_evidence_ndvi,
                        joined.carbon_total_potential_status,
                        joined.carbon_calculation_status
        """
    elif layer_name == "layer_b":
        layer_columns = """
                        joined.id,
                        joined.uuid,
                        joined.region_code,
                        joined.status AS verification_status,
                        joined.ndvi,
                        joined.cloud_coverage,
                        joined.observation_status AS status,
                        joined.statistics_source,
                        joined.observation_acquisition_date::text AS acquisition_date,
                        joined.forest_evidence_ndvi,
                        joined.carbon_total_potential_status,
                        joined.carbon_calculation_status
        """
    else:
        raise ValueError("Unsupported Stage 4 public tile layer")

    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        ),
        bounds AS (
            SELECT
                ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        ),
        tile_cells AS (
            SELECT
                cell.id,
                cell.uuid,
                cell.area_ha,
                cell.geom,
                region.code AS region_code
            FROM {grid_table} cell
            JOIN region_scope region ON region.id = cell.region_id
            CROSS JOIN bounds
            WHERE cell.region_id = (SELECT id FROM region_scope)
              AND cell.grid_version = :grid_version
              AND cell.geom && bounds.geom_4326
              AND ST_Intersects(cell.geom, bounds.geom_4326)
        ),
        joined AS (
            SELECT
                cell.id,
                cell.uuid::text AS uuid,
                cell.area_ha,
                cell.region_code,
                {_asset_verification_status_sql()} AS status,
                verification.score,
                COALESCE(observation.observation_status, 'unknown') AS observation_status,
                observation.acquisition_date AS observation_acquisition_date,
                observation.ndvi,
                observation.cloud_coverage,
                observation.observation_statistics_source AS statistics_source,
                COALESCE(observation.ndvi, -1) AS forest_evidence_ndvi,
                CASE
                    WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                    WHEN latest_carbon.status = 'calculated'
                      AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                        CASE
                            WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                            WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                            ELSE 'calculated'
                        END
                    ELSE latest_carbon.status
                END AS carbon_total_potential_status,
                COALESCE(latest_carbon.status, NULL::text)
                    AS carbon_calculation_status,
                cell.geom
            FROM tile_cells cell
            {_asset_stage4_public_verification_joins(include_audit=False)}
            LEFT JOIN LATERAL (
                {_asset_stage4_public_observation_sql(sentinel_table)}
            ) observation ON true
            LEFT JOIN LATERAL (
                {_asset_stage4_public_carbon_sql()}
            ) latest_carbon ON true
        ),
        mvtgeom AS (
            SELECT
                {layer_columns},
                ST_AsMVTGeom(
                    ST_Transform(
                        ST_CollectionExtract(ST_MakeValid(joined.geom), 3),
                        3857
                    ),
                    bounds.geom_3857,
                    4096,
                    64,
                    true
                ) AS geom
            FROM joined
            CROSS JOIN bounds
            WHERE (:status = 'all' OR joined.status = :status)
              AND (
                :carbon_status = 'all'
                OR joined.carbon_total_potential_status = :carbon_status
                OR joined.carbon_calculation_status = :carbon_status
              )
              AND (:signal_status = 'all' OR joined.observation_status = :signal_status)
              AND (
                CAST(:observed_from AS date) IS NULL
                OR joined.observation_acquisition_date >= CAST(:observed_from AS date)
              )
              AND (
                CAST(:observed_to AS date) IS NULL
                OR joined.observation_acquisition_date <= CAST(:observed_to AS date)
              )
              AND (
                :has_search IS false
                OR (
                    CAST(:search_cell_id AS bigint) IS NOT NULL
                    AND joined.id = CAST(:search_cell_id AS bigint)
                )
                OR (
                    CAST(:search_uuid_exact AS text) IS NOT NULL
                    AND joined.uuid = CAST(:search_uuid_exact AS text)
                )
                OR lower(joined.uuid) LIKE :search ESCAPE '\\'
                OR lower(replace(joined.uuid, '-', '')) LIKE :search ESCAPE '\\'
                OR lower(joined.region_code) LIKE :search ESCAPE '\\'
              )
        )
        SELECT ST_AsMVT(mvtgeom, '{layer_name}', 4096, 'geom')
        FROM mvtgeom
        WHERE geom IS NOT NULL
        """
    )


def _asset_stage4_public_unfiltered_query(grid_table: str):
    sentinel_table = sentinel_observations_table_sql()
    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        ),
        page_cells AS (
            SELECT cell.*
            FROM {grid_table} cell
            WHERE cell.region_id = (SELECT id FROM region_scope)
              AND cell.grid_version = :grid_version
            ORDER BY cell.id
            LIMIT :query_limit
            OFFSET :offset
        )
        SELECT {_asset_stage4_public_columns_sql("NULL::json")}
        FROM page_cells cell
        JOIN region_scope region ON region.id = cell.region_id
        {_asset_stage4_public_verification_joins()}
        LEFT JOIN LATERAL (
            {_asset_stage4_public_observation_sql(sentinel_table)}
        ) observation ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_sql()}
        ) latest_carbon ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_audit_sql()}
        ) carbon_audit_rollup ON true
        ORDER BY cell.id
        """
    )


def _asset_stage4_public_filtered_query(grid_table: str):
    sentinel_table = sentinel_observations_table_sql()
    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        )
        SELECT {_asset_stage4_public_columns_sql("NULL::json")}
        FROM {grid_table} cell
        JOIN region_scope region ON region.id = cell.region_id
        {_asset_stage4_public_verification_joins()}
        LEFT JOIN LATERAL (
            {_asset_stage4_public_observation_sql(sentinel_table)}
        ) observation ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_sql()}
        ) latest_carbon ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_audit_sql()}
        ) carbon_audit_rollup ON true
        WHERE cell.region_id = (SELECT id FROM region_scope)
          AND cell.grid_version = :grid_version
          AND {_asset_stage4_public_filter_where_sql()}
        ORDER BY cell.id
        LIMIT :query_limit
        OFFSET :offset
        """
    )


def _asset_stage4_public_detail_query(grid_table: str):
    sentinel_table = sentinel_observations_table_sql()
    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        )
        SELECT {_asset_stage4_public_columns_sql("ST_AsGeoJSON(cell.geom, 17)::json")}
        FROM {grid_table} cell
        JOIN region_scope region ON region.id = cell.region_id
        {_asset_stage4_public_verification_joins()}
        LEFT JOIN LATERAL (
            {_asset_stage4_public_observation_sql(sentinel_table)}
        ) observation ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_sql()}
        ) latest_carbon ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_audit_sql()}
        ) carbon_audit_rollup ON true
        WHERE cell.region_id = (SELECT id FROM region_scope)
          AND cell.grid_version = :grid_version
          AND cell.uuid = CAST(:cell_uuid AS uuid)
        LIMIT 1
        """
    )


def _asset_stage4_public_history_query(grid_table: str, sentinel_table: str):
    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        ),
        target_cell AS (
            SELECT cell.id, cell.uuid::text AS cell_uuid
            FROM {grid_table} cell
            WHERE cell.region_id = (SELECT id FROM region_scope)
              AND cell.grid_version = :grid_version
              AND cell.uuid = CAST(:cell_uuid AS uuid)
            LIMIT 1
        ),
        anchor_date AS (
            SELECT COALESCE(max(anchor.acquisition_date), CURRENT_DATE) AS acquisition_date
            FROM {sentinel_table} anchor
            WHERE anchor.grid_cell_id = (SELECT id FROM target_cell)
              AND anchor.metadata->>'pipeline' = 'stage4_layer_b_ndvi'
              AND anchor.region_id = (SELECT id FROM region_scope)
              AND anchor.region_code = :region_code
              AND anchor.grid_version = :grid_version
        )
        SELECT
            daily.id,
            daily.grid_cell_id,
            daily.cell_uuid,
            daily.acquisition_date::text AS acquisition_date,
            daily.ndvi,
            daily.cloud_coverage,
            daily.status,
            daily.statistics_source,
            daily.valid_pixel_count,
            daily.valid_pixel_ratio
        FROM (
            SELECT DISTINCT ON (observation.acquisition_date)
                observation.id,
                observation.grid_cell_id,
                cell.cell_uuid,
                observation.acquisition_date,
                observation.ndvi,
                observation.cloud_coverage,
                COALESCE(
                    {_observation_status_sql("observation")},
                    CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END,
                    'unknown'
                ) AS status,
                observation.metadata->>'statistics_source' AS statistics_source,
                CASE
                    WHEN observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                    THEN (observation.metadata->>'valid_pixel_count')::integer
                END AS valid_pixel_count,
                CASE
                    WHEN observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (observation.metadata->>'valid_pixel_ratio')::numeric
                END AS valid_pixel_ratio
            FROM target_cell cell
            CROSS JOIN anchor_date
            JOIN {sentinel_table} observation ON observation.grid_cell_id = cell.id
            WHERE observation.metadata->>'pipeline' = 'stage4_layer_b_ndvi'
              AND observation.region_id = (SELECT id FROM region_scope)
              AND observation.region_code = :region_code
              AND observation.grid_version = :grid_version
              AND observation.acquisition_date >= anchor_date.acquisition_date - (:months * INTERVAL '1 month')
            ORDER BY
                observation.acquisition_date,
                CASE WHEN observation.is_current THEN 0 ELSE 1 END,
                CASE
                    WHEN observation.metadata->>'status' = 'ok' THEN 0
                    WHEN observation.ndvi IS NOT NULL THEN 1
                    ELSE 2
                END,
                observation.id DESC
        ) daily
        ORDER BY daily.acquisition_date
        LIMIT 500
        """
    )


def _asset_verification_status_sql(verification_alias: str = "verification") -> str:
    # This EXISTS is uncorrelated: PostgreSQL can evaluate the scoped index probe
    # once per statement. A partially verified scope must not mix Layer C and
    # carbon classifications. Carbon remains the fallback for scopes with no C.
    verification_table = verification_results_table_sql()
    return f"""CASE WHEN EXISTS (
        SELECT 1
        FROM {verification_table} scope_result
        WHERE scope_result.region_id = (SELECT id FROM regions WHERE code = :region_code)
          AND scope_result.region_code = :region_code
          AND scope_result.grid_version = :grid_version
          AND scope_result.is_current
          AND scope_result.metadata->>'pipeline' = 'stage1_verification'
    ) THEN COALESCE({verification_alias}.anomaly_type, 'unverified')
      ELSE COALESCE(latest_carbon.metadata->>'anomaly_type', 'unverified') END"""


def _asset_stage4_public_verification_joins(*, include_audit: bool = True) -> str:
    verification_table = verification_results_table_sql()
    joins = f"""
        LEFT JOIN LATERAL (
            SELECT result.id, result.anomaly_type, result.score, result.explanation,
                result.metadata
            FROM {verification_table} result
            WHERE result.grid_cell_id = cell.id
              AND result.region_id = (SELECT id FROM region_scope)
              AND result.region_code = :region_code
              AND result.grid_version = :grid_version
              AND result.is_current
              AND result.metadata->>'pipeline' = 'stage1_verification'
            ORDER BY result.created_at DESC, result.id DESC
            LIMIT 1
        ) verification ON true
    """
    if include_audit:
        joins += """
            LEFT JOIN LATERAL (
                SELECT count(*)::bigint AS audit_event_count,
                    max(event.created_at)::text AS latest_event_at
                FROM verification_audit_events event
                WHERE event.grid_cell_id = cell.id
                  AND event.region_id = (SELECT id FROM region_scope)
                  AND event.region_code = :region_code
                  AND event.grid_version = :grid_version
                  AND event.metadata->>'pipeline' = 'stage1_verification_audit'
            ) verification_audit ON true
        """
    return joins


def _asset_stage4_public_observation_sql(sentinel_table: str) -> str:
    return f"""
        SELECT
            observation.acquisition_date,
            observation.ndvi,
            observation.cloud_coverage,
            COALESCE(
                {_observation_status_sql("observation")},
                CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END,
                'unknown'
            ) AS observation_status,
            observation.metadata->>'statistics_source' AS observation_statistics_source,
            CASE
                WHEN observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                THEN (observation.metadata->>'valid_pixel_count')::integer
            END AS observation_valid_pixel_count,
            CASE
                WHEN observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                THEN (observation.metadata->>'valid_pixel_ratio')::numeric
            END AS observation_valid_pixel_ratio,
            CASE
                WHEN observation.metadata->>'sample_count' ~ '^[0-9]+$'
                THEN (observation.metadata->>'sample_count')::integer
            END AS observation_sample_count
        FROM {sentinel_table} observation
        WHERE observation.grid_cell_id = cell.id
          AND observation.is_current
          AND observation.metadata->>'pipeline' = 'stage4_layer_b_ndvi'
          AND observation.region_id = (SELECT id FROM region_scope)
          AND observation.region_code = :region_code
          AND observation.grid_version = :grid_version
        ORDER BY observation.acquisition_date DESC, observation.id DESC
        LIMIT 1
    """


def _asset_stage4_public_carbon_sql() -> str:
    return f"""
        SELECT
            assessment.id,
            assessment.methodology_version,
            assessment.coefficient_set_version,
            assessment.soil_tco2e,
            assessment.biomass_tco2e,
            assessment.root_tco2e,
            assessment.total_potential_tco2e,
            assessment.assessment_year,
            assessment.previous_annual_assessment_id,
            assessment.gross_increment_tco2e_ha_yr,
            assessment.loss_factor_tco2e_ha_yr,
            assessment.net_annual_increment_tco2e_ha_yr,
            assessment.net_annual_increment_tco2e_yr,
            assessment.total_potential_delta_tco2e,
            assessment.net_annual_increment_delta_tco2e_ha_yr,
            assessment.net_annual_increment_delta_tco2e_yr,
            assessment.verification_factor,
            assessment.confidence_score,
            assessment.calibration_source,
            assessment.status,
            assessment.metadata,
            assessment.source_snapshot,
            assessment.created_at
        FROM carbon_assessments assessment
        WHERE assessment.grid_cell_id = cell.id
          AND assessment.is_current
          AND assessment.metadata->>'pipeline' IN {CARBON_PIPELINE_SQL}
        ORDER BY {CARBON_PIPELINE_PRIORITY_SQL}, assessment.created_at DESC, assessment.id DESC
        LIMIT 1
    """


def _asset_stage4_public_carbon_audit_sql() -> str:
    return f"""
        SELECT
            count(*)::bigint AS carbon_audit_event_count,
            max(event.created_at)::text AS latest_carbon_event_at
        FROM carbon_assessment_events event
        WHERE latest_carbon.id IS NOT NULL
          AND event.grid_cell_id = cell.id
          AND event.carbon_assessment_id = latest_carbon.id
          AND event.metadata->>'pipeline' IN {CARBON_AUDIT_PIPELINE_SQL}
    """


def _asset_stage4_public_filter_where_sql() -> str:
    return f"""
        (
            CAST(:status AS text) = 'all'
            OR {_asset_verification_status_sql()} = CAST(:status AS text)
        )
        AND (
            CAST(:carbon_status AS text) = 'all'
            OR CASE
                WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                WHEN latest_carbon.status = 'calculated'
                  AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                    CASE
                        WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                        WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                        ELSE 'calculated'
                    END
                ELSE latest_carbon.status
            END = CAST(:carbon_status AS text)
            OR COALESCE(latest_carbon.status, CAST(NULL::text AS text))
                = CAST(:carbon_status AS text)
        )
        AND (
            CAST(:signal_status AS text) = 'all'
            OR COALESCE(observation.observation_status, 'unknown') = CAST(:signal_status AS text)
        )
        AND (CAST(:observed_from AS date) IS NULL OR observation.acquisition_date::date >= CAST(:observed_from AS date))
        AND (CAST(:observed_to AS date) IS NULL OR observation.acquisition_date::date <= CAST(:observed_to AS date))
        AND (
            CAST(:has_search AS boolean) IS false
            OR lower(CAST(:region_code AS text)) LIKE :search ESCAPE '\\'
            OR {_asset_no_evidence_search_sql()}
        )
    """


def _asset_ids_stage4_public_filtered_query(grid_table: str):
    sentinel_table = sentinel_observations_table_sql()
    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        )
        SELECT cell.uuid::text AS cell_uuid
        FROM {grid_table} cell
        JOIN region_scope region ON region.id = cell.region_id
        {_asset_stage4_public_verification_joins(include_audit=False)}
        LEFT JOIN LATERAL (
            {_asset_stage4_public_observation_sql(sentinel_table)}
        ) observation ON true
        LEFT JOIN LATERAL (
            {_asset_stage4_public_carbon_sql()}
        ) latest_carbon ON true
        WHERE cell.region_id = (SELECT id FROM region_scope)
          AND cell.grid_version = :grid_version
          AND {_asset_stage4_public_filter_where_sql()}
        ORDER BY cell.id
        LIMIT :query_limit
        """
    )


def _asset_stage4_public_columns_sql(geometry_sql: str) -> str:
    return f"""
            cell.id,
            cell.uuid::text AS cell_uuid,
            region.code AS region_code,
            cell.area_ha,
            COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
            cell.grid_version,
            verification.id AS verification_result_id,
            {_asset_verification_status_sql()} AS anomaly_type,
            verification.score,
            COALESCE(verification.explanation, 'No verification result recorded for this asset yet.') AS explanation,
            observation.ndvi,
            observation.acquisition_date::text AS acquisition_date,
            {_stage5_json_numeric_sql("verification.metadata->>'previous_ndvi'")} AS previous_ndvi,
            verification.metadata->>'previous_acquisition_date' AS previous_acquisition_date,
            CASE WHEN verification.metadata->>'previous_observation_id' ~ '^[0-9]+$'
                THEN (verification.metadata->>'previous_observation_id')::bigint END AS previous_observation_id,
            verification.metadata->>'layer_a_relation' AS layer_a_relation,
            {_stage5_json_numeric_sql("verification.metadata->>'layer_a_intersection_area_ha'")} AS layer_a_intersection_area_ha,
            {_stage5_json_numeric_sql("verification.metadata->>'layer_a_coverage_ratio'")} AS layer_a_coverage_ratio,
            verification.metadata->>'layer_a_source_name' AS layer_a_source_name,
            verification.metadata->>'layer_a_source_quality' AS layer_a_source_quality,
            verification.metadata->>'layer_a_official_status' AS layer_a_official_status,
            verification.metadata->>'layer_a_source_checked_at' AS layer_a_source_checked_at,
            NULL::text AS layer_a_latest_check_at,
            NULL::text AS layer_a_latest_check_status,
            NULL::integer AS layer_a_latest_check_feature_count,
            NULL::integer AS layer_a_data_age_days,
            COALESCE(observation.observation_status, 'unknown') AS observation_status,
            observation.cloud_coverage AS observation_cloud_coverage,
            observation.observation_statistics_source,
            observation.observation_valid_pixel_count,
            observation.observation_valid_pixel_ratio,
            observation.observation_sample_count,
            COALESCE(verification_audit.audit_event_count, 0) AS audit_event_count,
            verification_audit.latest_event_at,
            latest_carbon.soil_tco2e AS carbon_soil_tco2e,
            latest_carbon.biomass_tco2e AS carbon_biomass_tco2e,
            latest_carbon.root_tco2e AS carbon_root_tco2e,
            latest_carbon.total_potential_tco2e AS carbon_total_potential_tco2e,
            {_stage5_assessment_year_sql()} AS carbon_assessment_year,
            latest_carbon.previous_annual_assessment_id AS carbon_previous_annual_assessment_id,
            {_stage5_gross_increment_sql()} AS carbon_gross_increment_tco2e_ha_yr,
            {_stage5_loss_factor_sql()} AS carbon_loss_factor_tco2e_ha_yr,
            {_stage5_net_annual_increment_ha_sql()} AS carbon_net_annual_increment_tco2e_ha_yr,
            {_stage5_net_annual_increment_asset_sql()} AS carbon_net_annual_increment_tco2e_yr,
            latest_carbon.total_potential_delta_tco2e AS carbon_total_potential_delta_tco2e,
            latest_carbon.net_annual_increment_delta_tco2e_ha_yr
                AS carbon_net_annual_increment_delta_tco2e_ha_yr,
            latest_carbon.net_annual_increment_delta_tco2e_yr AS carbon_net_annual_increment_delta_tco2e_yr,
            CAST(:carbon_unit AS text) AS carbon_unit,
            COALESCE(latest_carbon.metadata->'soil'->>'input_status', 'required') AS carbon_soil_input_status,
            COALESCE(latest_carbon.metadata->'biomass'->>'input_status', 'required') AS carbon_biomass_input_status,
            COALESCE(latest_carbon.metadata->'root_system'->>'input_status', 'required')
                AS carbon_root_input_status,
            CASE
                WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                WHEN latest_carbon.status = 'calculated'
                  AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                    CASE
                        WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                        WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                        ELSE 'calculated'
                    END
                ELSE latest_carbon.status
            END AS carbon_total_potential_status,
            COALESCE(latest_carbon.metadata->>'coefficient_status', CAST(NULL::text AS text))
                AS carbon_coefficient_status,
            CASE
                WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%grid_cell:%'
                THEN 'grid_cell_override'
                WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%region:%'
                THEN 'region_override'
                WHEN latest_carbon.id IS NULL THEN NULL
                ELSE 'global_baseline'
            END AS carbon_coefficient_scope,
            COALESCE(latest_carbon.methodology_version, CAST(NULL::text AS text))
                AS carbon_methodology_version,
            COALESCE(latest_carbon.status, CAST(NULL::text AS text))
                AS carbon_calculation_status,
            latest_carbon.coefficient_set_version AS carbon_coefficient_set_version,
            latest_carbon.verification_factor AS carbon_verification_factor,
            latest_carbon.confidence_score AS carbon_confidence_score,
            latest_carbon.calibration_source AS carbon_calibration_source,
            COALESCE(
                latest_carbon.metadata->>'increment_model',
                'baseline-ipcc-ogm-v0.1'
            ) AS carbon_increment_model,
            COALESCE(carbon_audit_rollup.carbon_audit_event_count, 0) AS carbon_audit_event_count,
            carbon_audit_rollup.latest_carbon_event_at,
            jsonb_build_object(
                'methodology_version', COALESCE(latest_carbon.methodology_version, CAST(NULL::text AS text)),
                'soil', jsonb_build_object(
                    'value', latest_carbon.soil_tco2e,
                    'unit', CAST(:carbon_unit AS text),
                    'input_status', COALESCE(latest_carbon.metadata->'soil'->>'input_status', 'required')
                ),
                'biomass', jsonb_build_object(
                    'value', latest_carbon.biomass_tco2e,
                    'unit', CAST(:carbon_unit AS text),
                    'input_status', COALESCE(latest_carbon.metadata->'biomass'->>'input_status', 'required')
                ),
                'root_system', jsonb_build_object(
                    'value', latest_carbon.root_tco2e,
                    'unit', CAST(:carbon_unit AS text),
                    'input_status', COALESCE(latest_carbon.metadata->'root_system'->>'input_status', 'required')
                ),
                'total_potential', jsonb_build_object(
                    'value', latest_carbon.total_potential_tco2e,
                    'unit', CAST(:carbon_unit AS text),
                    'status', CASE
                        WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                        WHEN latest_carbon.status = 'calculated'
                          AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                            CASE
                                WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                                WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                                ELSE 'calculated'
                            END
                        ELSE latest_carbon.status
                    END
                ),
                'annual_increment', jsonb_build_object(
                        'increment_model', COALESCE(
                            latest_carbon.metadata->>'increment_model',
                            'baseline-ipcc-ogm-v0.1'
                        ),
                        'unit', 'tCO2e/ha/year',
                        'asset_unit', 'tCO2e/year',
                        'input_unit', 'tC/ha/year',
                        'stoichiometric_factor', CAST(NULL::numeric AS numeric)::text,
                        'gross_increment', jsonb_build_object(
                            'value', {_stage5_gross_increment_sql()},
                            'unit', 'tCO2e/ha/year'
                        ),
                        'biomass_increment', jsonb_build_object(
                            'value', {_stage5_biomass_increment_sql()},
                            'unit', 'tCO2e/ha/year'
                        ),
                        'soil_increment', jsonb_build_object(
                            'value', {_stage5_soil_increment_sql()},
                            'unit', 'tCO2e/ha/year'
                        ),
                        'loss_factor', jsonb_build_object(
                            'value', {_stage5_loss_factor_sql()},
                            'unit', 'tCO2e/ha/year'
                        ),
                        'net_per_ha_year', jsonb_build_object(
                            'value', {_stage5_net_annual_increment_ha_sql()},
                            'unit', 'tCO2e/ha/year',
                            'status', latest_carbon.status
                        ),
                        'net_per_asset_year', jsonb_build_object(
                            'value', {_stage5_net_annual_increment_asset_sql()},
                            'unit', 'tCO2e/year',
                            'status', latest_carbon.status
                        ),
                        'net_delta_per_ha_year', jsonb_build_object(
                            'value', latest_carbon.net_annual_increment_delta_tco2e_ha_yr,
                            'unit', 'tCO2e/ha/year',
                            'status', COALESCE(
                                latest_carbon.metadata->'year_over_year'->>'comparison_state',
                                'awaiting_previous_carbon_snapshot'
                            )
                        ),
                        'net_delta_per_asset_year', jsonb_build_object(
                            'value', latest_carbon.net_annual_increment_delta_tco2e_yr,
                            'unit', 'tCO2e/year',
                            'status', COALESCE(
                                latest_carbon.metadata->'year_over_year'->>'comparison_state',
                                'awaiting_previous_carbon_snapshot'
                            )
                        )
                ),
                'verification_factor', latest_carbon.verification_factor,
                'verification_factor_status', COALESCE(
                    latest_carbon.metadata->>'verification_factor_status',
                    CAST(NULL::text AS text)
                ),
                'confidence_score', latest_carbon.confidence_score,
                'anomaly_type', latest_carbon.metadata->>'anomaly_type',
                'calibration_source', latest_carbon.calibration_source,
                'layer_d_status', CAST(NULL::text AS text),
                'audit_event_count', COALESCE(carbon_audit_rollup.carbon_audit_event_count, 0),
                'latest_event_at', carbon_audit_rollup.latest_carbon_event_at,
                'evidence_state', CASE
                    WHEN latest_carbon.metadata->>'evidence_state' IS NOT NULL
                    THEN latest_carbon.metadata->>'evidence_state'
                    WHEN observation.ndvi IS NULL THEN 'awaiting_satellite_signal'
                    ELSE 'ready'
                END,
                'methodology_state', COALESCE(
                    latest_carbon.metadata->>'coefficient_status',
                    CAST(NULL::text AS text)
                ),
                'calculation_state', COALESCE(latest_carbon.status, CAST(NULL::text AS text)),
                'last_calculated_at', latest_carbon.created_at::text,
                'trend_interval_days', latest_carbon.metadata->>'trend_interval_days',
                'year_over_year', COALESCE(
                    latest_carbon.metadata->'year_over_year',
                    {_stage5_year_over_year_fallback_sql()}
                ),
                'coefficient_set_version', latest_carbon.coefficient_set_version,
                'coefficient_scope', CASE
                    WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%grid_cell:%'
                    THEN 'grid_cell_override'
                    WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%region:%'
                    THEN 'region_override'
                    WHEN latest_carbon.id IS NULL THEN NULL
                    ELSE 'global_baseline'
                END,
                'coefficient_status', COALESCE(latest_carbon.metadata->>'coefficient_status', CAST(NULL::text AS text)),
                'calculation_status', COALESCE(latest_carbon.status, CAST(NULL::text AS text))
            ) AS carbon_readiness,
            latest_carbon.created_at,
            {geometry_sql} AS geometry
    """


def _asset_count_query(grid_table: str):
    return text(
        f"""
        {_asset_scope_sql(grid_table)}
        SELECT count(*)
        FROM assets
        WHERE (:status = 'all' OR anomaly_type = :status)
          AND (:carbon_status = 'all' OR carbon_total_potential_status = :carbon_status OR carbon_calculation_status = :carbon_status)
          AND (:signal_status = 'all' OR observation_status = :signal_status)
          AND (CAST(:observed_from AS date) IS NULL OR acquisition_date::date >= CAST(:observed_from AS date))
          AND (CAST(:observed_to AS date) IS NULL OR acquisition_date::date <= CAST(:observed_to AS date))
          AND (
            :has_search IS false
            OR lower(cell_uuid) LIKE :search ESCAPE '\\'
            OR lower(region_code) LIKE :search ESCAPE '\\'
          )
        """
    )


def _asset_no_evidence_count_query(grid_table: str):
    return text(
        f"""
        SELECT CASE
            WHEN CAST(:synthetic_filters_match AS boolean) IS false THEN 0::bigint
            WHEN CAST(:has_search AS boolean) IS false OR lower(CAST(:region_code AS text)) LIKE :search ESCAPE '\\'
            THEN (
                {_asset_scope_total_sql(grid_table)}
            )
            ELSE (
                SELECT count(*)
                FROM {grid_table} cell
                WHERE cell.region_id = (
                      SELECT id
                      FROM regions
                      WHERE code = :region_code
                  )
                  AND cell.grid_version = :grid_version
                  AND {_asset_no_evidence_search_sql()}
            )
        END
        """
    )


def _scope_has_verification_evidence(db, params: dict[str, object]) -> bool:
    verification_table = verification_results_table_sql()
    return bool(
        db.execute(
            text(
                f"""
                WITH region_scope AS (
                    SELECT id
                    FROM regions
                    WHERE code = :region_code
                )
                SELECT EXISTS (
                    SELECT 1
                    FROM {verification_table} result
                    WHERE result.is_current
                      AND result.metadata->>'pipeline' = 'stage1_verification'
                      AND result.region_id = (SELECT id FROM region_scope)
                      AND result.region_code = :region_code
                      AND result.grid_version = :grid_version
                    LIMIT 1
                )
                """
            ),
            params,
        ).scalar_one()
    )


def _asset_filter_params(
    *,
    status: str,
    carbon_status: str,
    signal_status: str,
    observed_from: str,
    observed_to: str,
    search: str,
) -> dict[str, object]:
    status = status.strip().lower()
    if status not in ASSET_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported asset status")
    carbon_status = carbon_status.strip().lower()
    if carbon_status not in CARBON_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported carbon status")
    signal_status = signal_status.strip().lower()
    if signal_status not in SIGNAL_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported signal status")
    observed_from = observed_from.strip()
    observed_to = observed_to.strip()
    if observed_from and not valid_iso_date(observed_from):
        raise HTTPException(status_code=422, detail="Unsupported observed_from date")
    if observed_to and not valid_iso_date(observed_to):
        raise HTTPException(status_code=422, detail="Unsupported observed_to date")
    search_text = search.strip()
    search_value = _escape_like(search_text.lower())
    return {
        "status": status,
        "carbon_status": carbon_status,
        "signal_status": signal_status,
        "observed_from": observed_from or None,
        "observed_to": observed_to or None,
        "search": f"%{search_value}%",
        "has_search": bool(search_text),
        **_asset_search_params(search_text),
    }


def _asset_search_params(search_text: str) -> dict[str, object]:
    search_text = search_text.strip().lower()
    compact = search_text.replace("-", "")
    search_cell_id = None
    if search_text.isdecimal():
        cell_id = int(search_text)
        if cell_id <= 9_223_372_036_854_775_807:
            search_cell_id = cell_id

    search_uuid_exact = None
    if search_text:
        try:
            search_uuid_exact = str(UUID(search_text))
        except ValueError:
            search_uuid_exact = None

    has_uuid_prefix = False
    uuid_prefix_start = None
    uuid_prefix_end = None
    if compact and len(compact) < 32 and all(char in "0123456789abcdef" for char in compact):
        has_uuid_prefix = True
        uuid_prefix_start = _format_uuid_hex(compact.ljust(32, "0"))
        uuid_prefix_end = _format_uuid_hex(compact.ljust(32, "f"))

    return {
        "search_cell_id": search_cell_id,
        "search_uuid_exact": search_uuid_exact,
        "has_uuid_prefix": has_uuid_prefix,
        "uuid_prefix_start": uuid_prefix_start,
        "uuid_prefix_end": uuid_prefix_end,
    }


def _format_uuid_hex(value: str) -> str:
    return f"{value[0:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:32]}"


def _asset_no_evidence_filter_params(params: dict[str, object]) -> dict[str, object]:
    enriched = dict(params)
    enriched["synthetic_filters_match"] = _asset_no_evidence_filters_match(params)
    return enriched


def _asset_no_evidence_filters_match(params: dict[str, object]) -> bool:
    return (
        params["status"] in {"all", "unverified"}
        and params["carbon_status"]
        in {"all", "awaiting_coefficients", params["carbon_calculation_status"]}
        and params["signal_status"] in {"all", "unknown"}
        and params["observed_from"] is None
        and params["observed_to"] is None
    )


def _asset_stage4_public_filters_match(params: dict[str, object]) -> bool:
    return True


def _asset_stage4_public_summary_total(db, params: dict[str, object]) -> int | None:
    if (
        params["signal_status"] != "all"
        or params["observed_from"] is not None
        or params["observed_to"] is not None
        or bool(params["has_search"])
    ):
        return None
    row = db.execute(_asset_stage4_public_stage5_summary_sql(), params).mappings().one_or_none()
    if row is None:
        return None
    return _asset_stage4_public_summary_count(row, params)


def _asset_stage4_public_stage5_summary_sql():
    return text(
        f"""
        SELECT
            total_cells,
            carbon_assessments,
            calculated_cells,
            skipped_missing_evidence_cells,
            baseline_cells,
            ok_cells,
            watch_cells,
            anomaly_cells,
            insufficient_data_cells,
            unverified_cells
        FROM stage5_carbon_summaries
        WHERE region_code = :region_code
          AND grid_version = :grid_version
          AND pipeline = '{STAGE5_CARBON_PIPELINE_NAME}'
        ORDER BY refreshed_at DESC, id DESC
        LIMIT 1
        """
    )


def _asset_stage4_public_summary_count(summary: dict[str, object], params: dict[str, object]) -> int | None:
    status = str(params["status"])
    carbon_status = str(params["carbon_status"])
    calculated_cells = int(summary["calculated_cells"] or 0)
    baseline_cells = int(summary["baseline_cells"] or 0)
    by_status = {
        "ok": int(summary["ok_cells"] or 0),
        "watch": int(summary["watch_cells"] or 0),
        "anomaly": int(summary["anomaly_cells"] or 0),
        "insufficient_data": int(summary["insufficient_data_cells"] or 0),
        "unverified": int(summary["unverified_cells"] or 0),
    }
    if status == "all":
        status_count = int(summary["carbon_assessments"] or summary["total_cells"] or 0)
    else:
        status_count = by_status.get(status, 0)

    if carbon_status == "all":
        return status_count
    if carbon_status == "calculated":
        if status == "all":
            return calculated_cells
        if status in {"ok", "watch", "anomaly"}:
            return status_count
        return 0
    if carbon_status == "baseline_estimate":
        baseline_calculated_cells = min(baseline_cells, calculated_cells)
        if status == "all":
            return baseline_calculated_cells
        if baseline_cells >= calculated_cells and status in {"ok", "watch", "anomaly"}:
            return status_count
        return None
    if carbon_status == "draft_estimate":
        if baseline_cells >= calculated_cells:
            return 0
        return None
    if carbon_status == "skipped_missing_evidence":
        if status in {"all", "insufficient_data"}:
            return int(summary["skipped_missing_evidence_cells"] or 0)
        return 0
    if carbon_status in {"awaiting_coefficients", "pending_coefficients", "pending_inputs", "failed"}:
        return 0
    return status_count


def _paged_total(rows: list[object], *, limit: int, offset: int) -> tuple[int, bool, bool]:
    if not rows and offset:
        # An out-of-range offset is not evidence of that many matching rows.
        return 0, False, False
    has_more = len(rows) > limit
    returned = min(len(rows), limit)
    return offset + returned + (1 if has_more else 0), not has_more, has_more


def _tile_params(
    *,
    z: int,
    x: int,
    y: int,
    status: str,
    carbon_status: str,
    signal_status: str,
    observed_from: str,
    observed_to: str,
    search: str,
    region_code: str | None,
    grid_version: str | None,
) -> dict[str, object]:
    return {
        **stage1_params(region_code=region_code, grid_version=grid_version),
        **_carbon_api_params(),
        **_asset_filter_params(
            status=status,
            carbon_status=carbon_status,
            signal_status=signal_status,
            observed_from=observed_from,
            observed_to=observed_to,
            search=search,
        ),
        "z": z,
        "x": x,
        "y": y,
    }


def _asset_scope_count_query(grid_table: str = "grid_cells"):
    return text(
        f"""
        SELECT count(*)
        FROM {grid_table} cell
        WHERE cell.region_id = (
              SELECT id
              FROM regions
              WHERE code = :region_code
          )
          AND cell.grid_version = :grid_version
        """
    )


def _asset_scope_total_query(grid_table: str):
    return text(
        f"""
        SELECT {_asset_scope_total_sql(grid_table)}
        """
    )


def _asset_scope_total_sql(grid_table: str) -> str:
    return f"""
        COALESCE(
            (
                SELECT run.inserted_cells
                FROM stage3_grid_generation_runs run
                WHERE run.region_id = (
                      SELECT id
                      FROM regions
                      WHERE code = :region_code
                  )
                  AND run.grid_version = :grid_version
                  AND run.status = 'completed'
                  AND run.inserted_cells IS NOT NULL
                ORDER BY run.finished_at DESC NULLS LAST, run.id DESC
                LIMIT 1
            ),
            (
                SELECT count(*)
                FROM {grid_table} cell
                WHERE cell.region_id = (
                      SELECT id
                      FROM regions
                      WHERE code = :region_code
                  )
                  AND cell.grid_version = :grid_version
            )
        )
    """


def _has_asset_filters(params: dict[str, object]) -> bool:
    return (
        params["status"] != "all"
        or params["carbon_status"] != "all"
        or params["signal_status"] != "all"
        or params["observed_from"] is not None
        or params["observed_to"] is not None
        or bool(params["has_search"])
    )


def _asset_no_evidence_where_sql() -> str:
    return f"""
        CAST(:synthetic_filters_match AS boolean) IS true
        AND (
            CAST(:has_search AS boolean) IS false
            OR lower(CAST(:region_code AS text)) LIKE :search ESCAPE '\\'
            OR {_asset_no_evidence_search_sql()}
        )
    """


def _asset_no_evidence_search_sql() -> str:
    return """
        (
            (CAST(:search_cell_id AS bigint) IS NOT NULL AND cell.id = CAST(:search_cell_id AS bigint))
            OR (CAST(:search_uuid_exact AS uuid) IS NOT NULL AND cell.uuid = CAST(:search_uuid_exact AS uuid))
            OR (
                CAST(:has_uuid_prefix AS boolean) IS true
                AND cell.uuid >= CAST(:uuid_prefix_start AS uuid)
                AND cell.uuid <= CAST(:uuid_prefix_end AS uuid)
            )
        )
    """


def _asset_ids_query(grid_table: str):
    return text(
        f"""
        {_asset_scope_sql(grid_table)}
        SELECT cell_uuid
        FROM assets
        WHERE (:status = 'all' OR anomaly_type = :status)
          AND (:carbon_status = 'all' OR carbon_total_potential_status = :carbon_status OR carbon_calculation_status = :carbon_status)
          AND (:signal_status = 'all' OR observation_status = :signal_status)
          AND (CAST(:observed_from AS date) IS NULL OR acquisition_date::date >= CAST(:observed_from AS date))
          AND (CAST(:observed_to AS date) IS NULL OR acquisition_date::date <= CAST(:observed_to AS date))
          AND (
            :has_search IS false
            OR lower(cell_uuid) LIKE :search ESCAPE '\\'
            OR lower(region_code) LIKE :search ESCAPE '\\'
          )
        ORDER BY cell_uuid
        LIMIT :limit
        """
    )


def _asset_ids_unfiltered_query(grid_table: str):
    return text(
        f"""
        SELECT cell.uuid::text AS cell_uuid
        FROM {grid_table} cell
        WHERE cell.region_id = (
              SELECT id
              FROM regions
              WHERE code = :region_code
          )
          AND cell.grid_version = :grid_version
        ORDER BY cell.uuid
        LIMIT :limit
        """
    )


def _asset_ids_no_evidence_query(grid_table: str):
    return text(
        f"""
        SELECT cell.uuid::text AS cell_uuid
        FROM {grid_table} cell
        WHERE cell.region_id = (
              SELECT id
              FROM regions
              WHERE code = :region_code
          )
          AND cell.grid_version = :grid_version
          AND {_asset_no_evidence_where_sql()}
        ORDER BY cell.uuid
        LIMIT :limit
        """
    )


def _asset_scope_sql(grid_table: str, *, target_cell: bool = False):
    sentinel_table = sentinel_observations_table_sql()
    verification_table = verification_results_table_sql()
    target_cell_cte = (
        f"""
        target_cell AS (
            SELECT cell.id
            FROM {grid_table} cell
            WHERE cell.region_id = (SELECT id FROM region_scope)
              AND cell.grid_version = :grid_version
              AND cell.uuid = CAST(:cell_uuid AS uuid)
            LIMIT 1
        ),
        """
        if target_cell
        else ""
    )
    target_cell_filter = "AND {column} = (SELECT id FROM target_cell)" if target_cell else ""
    target_assets_filter = "AND cell.id = (SELECT id FROM target_cell)" if target_cell else ""
    return f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        ),
        {target_cell_cte}
        latest_observation AS (
            SELECT DISTINCT ON (observation.grid_cell_id)
                observation.grid_cell_id,
                observation.id AS observation_id,
                observation.acquisition_date,
                observation.ndvi,
                observation.cloud_coverage,
                COALESCE(
                    {_observation_status_sql("observation")},
                    CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END
                ) AS observation_status,
                observation.metadata->>'statistics_source' AS observation_statistics_source,
                CASE
                    WHEN observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                    THEN (observation.metadata->>'valid_pixel_count')::integer
                END AS observation_valid_pixel_count,
                CASE
                    WHEN observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (observation.metadata->>'valid_pixel_ratio')::numeric
                END AS observation_valid_pixel_ratio,
                CASE
                    WHEN observation.metadata->>'sample_count' ~ '^[0-9]+$'
                    THEN (observation.metadata->>'sample_count')::integer
                END AS observation_sample_count
            FROM {sentinel_table} observation
            WHERE observation.is_current
              AND observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}
              AND observation.region_id = (SELECT id FROM region_scope)
              AND observation.region_code = :region_code
              AND observation.grid_version = :grid_version
              {target_cell_filter.format(column="observation.grid_cell_id")}
            ORDER BY observation.grid_cell_id, observation.acquisition_date DESC, {NDVI_PIPELINE_PRIORITY_SQL}, observation.id DESC
        ),
        latest_checks AS (
            SELECT DISTINCT ON (source_layer)
                source_layer,
                checked_at,
                status,
                feature_count
            FROM source_checks
            WHERE region_code = :layer_a_region_code
            ORDER BY source_layer, checked_at DESC, id DESC
        ),
        latest_result AS (
            SELECT DISTINCT ON (result.grid_cell_id)
                result.id,
                result.grid_cell_id,
                result.ogm_parcel_id,
                result.observation_id,
                result.anomaly_type,
                result.score,
                result.explanation,
                result.metadata,
                result.created_at
            FROM {verification_table} result
            WHERE result.is_current
              AND result.metadata->>'pipeline' = 'stage1_verification'
              AND result.region_id = (SELECT id FROM region_scope)
              AND result.region_code = :region_code
              AND result.grid_version = :grid_version
              {target_cell_filter.format(column="result.grid_cell_id")}
            ORDER BY result.grid_cell_id, result.created_at DESC, result.id DESC
        ),
        audit_rollup AS (
            SELECT
                event.grid_cell_id,
                count(*) AS audit_event_count,
                max(event.created_at) AS latest_event_at
            FROM verification_audit_events event
            WHERE event.metadata->>'pipeline' = 'stage1_verification_audit'
              AND event.region_id = (SELECT id FROM region_scope)
              AND event.region_code = :region_code
              AND event.grid_version = :grid_version
              {target_cell_filter.format(column="event.grid_cell_id")}
            GROUP BY event.grid_cell_id
        ),
        latest_carbon AS (
            SELECT DISTINCT ON (assessment.grid_cell_id)
                assessment.id,
                assessment.grid_cell_id,
                assessment.methodology_version,
                assessment.coefficient_set_version,
                assessment.soil_tco2e,
                assessment.biomass_tco2e,
                assessment.root_tco2e,
                assessment.total_potential_tco2e,
                assessment.assessment_year,
                assessment.previous_annual_assessment_id,
                assessment.gross_increment_tco2e_ha_yr,
                assessment.loss_factor_tco2e_ha_yr,
                assessment.net_annual_increment_tco2e_ha_yr,
                assessment.net_annual_increment_tco2e_yr,
                assessment.total_potential_delta_tco2e,
                assessment.net_annual_increment_delta_tco2e_ha_yr,
                assessment.net_annual_increment_delta_tco2e_yr,
                assessment.verification_factor,
                assessment.confidence_score,
                assessment.calibration_source,
                assessment.config_hash,
                assessment.status,
                assessment.metadata,
                assessment.source_snapshot,
                assessment.created_at
            FROM carbon_assessments assessment
            JOIN {grid_table} carbon_cell ON carbon_cell.id = assessment.grid_cell_id
            WHERE assessment.is_current
              AND assessment.metadata->>'pipeline' IN {CARBON_PIPELINE_SQL}
              AND carbon_cell.region_id = (SELECT id FROM region_scope)
              AND carbon_cell.grid_version = :grid_version
              {target_cell_filter.format(column="assessment.grid_cell_id")}
            ORDER BY assessment.grid_cell_id, {CARBON_PIPELINE_PRIORITY_SQL}, assessment.created_at DESC, assessment.id DESC
        ),
        carbon_audit_rollup AS (
            SELECT
                event.grid_cell_id,
                event.carbon_assessment_id,
                count(*) AS carbon_audit_event_count,
                max(event.created_at)::text AS latest_carbon_event_at
            FROM carbon_assessment_events event
            JOIN {grid_table} carbon_event_cell ON carbon_event_cell.id = event.grid_cell_id
            WHERE event.metadata->>'pipeline' IN {CARBON_AUDIT_PIPELINE_SQL}
              AND carbon_event_cell.region_id = (SELECT id FROM region_scope)
              AND carbon_event_cell.grid_version = :grid_version
              {target_cell_filter.format(column="event.grid_cell_id")}
            GROUP BY event.grid_cell_id, event.carbon_assessment_id
        ),
        assets AS (
            SELECT
                cell.id,
                cell.uuid::text AS cell_uuid,
                region.code AS region_code,
                cell.area_ha,
                COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient,
                cell.grid_version,
                latest_result.id AS verification_result_id,
                {_asset_verification_status_sql("latest_result")} AS anomaly_type,
                latest_result.score,
                COALESCE(latest_result.explanation, 'No verification result recorded for this asset yet.')
                    AS explanation,
                CASE
                    WHEN latest_result.id IS NOT NULL THEN result_observation.ndvi
                    ELSE latest_observation.ndvi
                END AS ndvi,
                CASE
                    WHEN latest_result.id IS NOT NULL THEN result_observation.acquisition_date
                    ELSE latest_observation.acquisition_date
                END::text AS acquisition_date,
                CASE
                    WHEN latest_result.metadata->>'previous_ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    THEN (latest_result.metadata->>'previous_ndvi')::numeric
                END AS previous_ndvi,
                GREATEST(
                    COALESCE(
                        CASE
                            WHEN latest_result.id IS NOT NULL THEN result_observation.ndvi
                            ELSE latest_observation.ndvi
                        END,
                        -1
                    ),
                    COALESCE(
                        CASE
                            WHEN latest_result.metadata->>'previous_ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                            THEN (latest_result.metadata->>'previous_ndvi')::numeric
                        END,
                        -1
                    )
                ) AS forest_evidence_ndvi,
                latest_result.metadata->>'previous_acquisition_date' AS previous_acquisition_date,
                CASE
                    WHEN latest_result.metadata->>'previous_observation_id' ~ '^[0-9]+$'
                    THEN (latest_result.metadata->>'previous_observation_id')::integer
                END AS previous_observation_id,
                latest_result.metadata->>'layer_a_relation' AS layer_a_relation,
                CASE
                    WHEN latest_result.metadata->>'layer_a_intersection_area_ha' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (latest_result.metadata->>'layer_a_intersection_area_ha')::numeric
                END AS layer_a_intersection_area_ha,
                CASE
                    WHEN latest_result.metadata->>'layer_a_coverage_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (latest_result.metadata->>'layer_a_coverage_ratio')::numeric
                END AS layer_a_coverage_ratio,
                latest_result.metadata->>'layer_a_source_name' AS layer_a_source_name,
                latest_result.metadata->>'layer_a_source_quality' AS layer_a_source_quality,
                latest_result.metadata->>'layer_a_official_status' AS layer_a_official_status,
                latest_result.metadata->>'layer_a_source_checked_at' AS layer_a_source_checked_at,
                latest_check.checked_at::text AS layer_a_latest_check_at,
                latest_check.status AS layer_a_latest_check_status,
                latest_check.feature_count AS layer_a_latest_check_feature_count,
                CASE
                    WHEN latest_result.metadata->>'layer_a_source_checked_at' IS NULL
                      OR latest_result.metadata->>'layer_a_source_checked_at' !~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                    THEN NULL
                    ELSE GREATEST(
                        0,
                        EXTRACT(
                            DAY FROM (
                                now() - (latest_result.metadata->>'layer_a_source_checked_at')::timestamptz
                            )
                        )::int
                    )
                END AS layer_a_data_age_days,
                COALESCE(
                    CASE
                        WHEN latest_result.id IS NOT NULL THEN COALESCE(
                            {_observation_status_sql("result_observation")},
                            CASE WHEN result_observation.ndvi IS NOT NULL THEN 'ok' END
                        )
                        ELSE latest_observation.observation_status
                    END,
                    'unknown'
                ) AS observation_status,
                CASE
                    WHEN latest_result.id IS NOT NULL THEN result_observation.cloud_coverage
                    ELSE latest_observation.cloud_coverage
                END AS observation_cloud_coverage,
                CASE
                    WHEN latest_result.id IS NOT NULL THEN result_observation.metadata->>'statistics_source'
                    ELSE latest_observation.observation_statistics_source
                END AS observation_statistics_source,
                CASE
                    WHEN latest_result.id IS NOT NULL
                      AND result_observation.metadata->>'valid_pixel_count' ~ '^[0-9]+$'
                    THEN (result_observation.metadata->>'valid_pixel_count')::integer
                    WHEN latest_result.id IS NULL THEN latest_observation.observation_valid_pixel_count
                END AS observation_valid_pixel_count,
                CASE
                    WHEN latest_result.id IS NOT NULL
                      AND result_observation.metadata->>'valid_pixel_ratio' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (result_observation.metadata->>'valid_pixel_ratio')::numeric
                    WHEN latest_result.id IS NULL THEN latest_observation.observation_valid_pixel_ratio
                END AS observation_valid_pixel_ratio,
                CASE
                    WHEN latest_result.id IS NOT NULL
                      AND result_observation.metadata->>'sample_count' ~ '^[0-9]+$'
                    THEN (result_observation.metadata->>'sample_count')::integer
                    WHEN latest_result.id IS NULL THEN latest_observation.observation_sample_count
                END AS observation_sample_count,
                COALESCE(audit_rollup.audit_event_count, 0) AS audit_event_count,
                audit_rollup.latest_event_at::text AS latest_event_at,
                latest_carbon.soil_tco2e AS carbon_soil_tco2e,
                latest_carbon.biomass_tco2e AS carbon_biomass_tco2e,
                latest_carbon.root_tco2e AS carbon_root_tco2e,
                latest_carbon.total_potential_tco2e AS carbon_total_potential_tco2e,
                {_stage5_assessment_year_sql()} AS carbon_assessment_year,
                latest_carbon.previous_annual_assessment_id AS carbon_previous_annual_assessment_id,
                {_stage5_gross_increment_sql()} AS carbon_gross_increment_tco2e_ha_yr,
                {_stage5_loss_factor_sql()} AS carbon_loss_factor_tco2e_ha_yr,
                {_stage5_net_annual_increment_ha_sql()} AS carbon_net_annual_increment_tco2e_ha_yr,
                {_stage5_net_annual_increment_asset_sql()} AS carbon_net_annual_increment_tco2e_yr,
                latest_carbon.total_potential_delta_tco2e AS carbon_total_potential_delta_tco2e,
                latest_carbon.net_annual_increment_delta_tco2e_ha_yr
                    AS carbon_net_annual_increment_delta_tco2e_ha_yr,
                latest_carbon.net_annual_increment_delta_tco2e_yr AS carbon_net_annual_increment_delta_tco2e_yr,
                CAST(:carbon_unit AS text) AS carbon_unit,
                COALESCE(latest_carbon.metadata->'soil'->>'input_status', 'required') AS carbon_soil_input_status,
                COALESCE(latest_carbon.metadata->'biomass'->>'input_status', 'required') AS carbon_biomass_input_status,
                COALESCE(latest_carbon.metadata->'root_system'->>'input_status', 'required') AS carbon_root_input_status,
                CASE
                    WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                    WHEN latest_carbon.status = 'calculated'
                      AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                        CASE
                            WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                            WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                            ELSE 'calculated'
                        END
                    ELSE latest_carbon.status
                END AS carbon_total_potential_status,
                COALESCE(latest_carbon.metadata->>'coefficient_status', CAST(NULL::text AS text))
                    AS carbon_coefficient_status,
                CASE
                    WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%grid_cell:%'
                    THEN 'grid_cell_override'
                    WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%region:%'
                    THEN 'region_override'
                    WHEN latest_carbon.id IS NULL THEN NULL
                    ELSE 'global_baseline'
                END AS carbon_coefficient_scope,
                COALESCE(latest_carbon.methodology_version, CAST(NULL::text AS text))
                    AS carbon_methodology_version,
                COALESCE(latest_carbon.status, CAST(NULL::text AS text))
                    AS carbon_calculation_status,
                latest_carbon.coefficient_set_version AS carbon_coefficient_set_version,
                latest_carbon.verification_factor AS carbon_verification_factor,
                latest_carbon.confidence_score AS carbon_confidence_score,
                latest_carbon.calibration_source AS carbon_calibration_source,
                COALESCE(
                    latest_carbon.metadata->>'increment_model',
                    'baseline-ipcc-ogm-v0.1'
                ) AS carbon_increment_model,
                COALESCE(carbon_audit_rollup.carbon_audit_event_count, 0) AS carbon_audit_event_count,
                carbon_audit_rollup.latest_carbon_event_at,
                jsonb_build_object(
                    'methodology_version', COALESCE(latest_carbon.methodology_version, CAST(NULL::text AS text)),
                    'soil', jsonb_build_object(
                        'value', latest_carbon.soil_tco2e,
                        'unit', CAST(:carbon_unit AS text),
                        'input_status', COALESCE(latest_carbon.metadata->'soil'->>'input_status', 'required')
                    ),
                    'biomass', jsonb_build_object(
                        'value', latest_carbon.biomass_tco2e,
                        'unit', CAST(:carbon_unit AS text),
                        'input_status', COALESCE(latest_carbon.metadata->'biomass'->>'input_status', 'required')
                    ),
                    'root_system', jsonb_build_object(
                        'value', latest_carbon.root_tco2e,
                        'unit', CAST(:carbon_unit AS text),
                        'input_status', COALESCE(latest_carbon.metadata->'root_system'->>'input_status', 'required')
                    ),
                    'total_potential', jsonb_build_object(
                        'value', latest_carbon.total_potential_tco2e,
                        'unit', CAST(:carbon_unit AS text),
                        'status', CASE
                            WHEN latest_carbon.id IS NULL THEN 'awaiting_coefficients'
                            WHEN latest_carbon.status = 'calculated'
                              AND latest_carbon.total_potential_tco2e IS NOT NULL THEN
                                CASE
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'baseline' THEN 'baseline_estimate'
                                    WHEN latest_carbon.metadata->>'coefficient_status' = 'draft' THEN 'draft_estimate'
                                    ELSE 'calculated'
                                END
                            ELSE latest_carbon.status
                        END
                    ),
                    'annual_increment', jsonb_build_object(
                            'increment_model', COALESCE(
                                latest_carbon.metadata->>'increment_model',
                                'baseline-ipcc-ogm-v0.1'
                            ),
                            'unit', 'tCO2e/ha/year',
                            'asset_unit', 'tCO2e/year',
                            'input_unit', 'tC/ha/year',
                            'stoichiometric_factor', CAST(NULL::numeric AS numeric)::text,
                            'gross_increment', jsonb_build_object(
                                'value', {_stage5_gross_increment_sql()},
                                'unit', 'tCO2e/ha/year'
                            ),
                            'biomass_increment', jsonb_build_object(
                                'value', {_stage5_biomass_increment_sql()},
                                'unit', 'tCO2e/ha/year'
                            ),
                            'soil_increment', jsonb_build_object(
                                'value', {_stage5_soil_increment_sql()},
                                'unit', 'tCO2e/ha/year'
                            ),
                            'loss_factor', jsonb_build_object(
                                'value', {_stage5_loss_factor_sql()},
                                'unit', 'tCO2e/ha/year'
                            ),
                            'net_per_ha_year', jsonb_build_object(
                                'value', {_stage5_net_annual_increment_ha_sql()},
                                'unit', 'tCO2e/ha/year',
                                'status', latest_carbon.status
                            ),
                            'net_per_asset_year', jsonb_build_object(
                                'value', {_stage5_net_annual_increment_asset_sql()},
                                'unit', 'tCO2e/year',
                                'status', latest_carbon.status
                            ),
                            'net_delta_per_ha_year', jsonb_build_object(
                                'value', latest_carbon.net_annual_increment_delta_tco2e_ha_yr,
                                'unit', 'tCO2e/ha/year',
                                'status', COALESCE(
                                    latest_carbon.metadata->'year_over_year'->>'comparison_state',
                                    'awaiting_previous_carbon_snapshot'
                                )
                            ),
                            'net_delta_per_asset_year', jsonb_build_object(
                                'value', latest_carbon.net_annual_increment_delta_tco2e_yr,
                                'unit', 'tCO2e/year',
                                'status', COALESCE(
                                    latest_carbon.metadata->'year_over_year'->>'comparison_state',
                                    'awaiting_previous_carbon_snapshot'
                                )
                            )
                    ),
                    'verification_factor', latest_carbon.verification_factor,
                    'verification_factor_status', COALESCE(
                        latest_carbon.metadata->>'verification_factor_status',
                        CAST(NULL::text AS text)
                    ),
                    'confidence_score', latest_carbon.confidence_score,
                    'anomaly_type', latest_carbon.metadata->>'anomaly_type',
                    'calibration_source', latest_carbon.calibration_source,
                    'layer_d_status', CAST(NULL::text AS text),
                    'audit_event_count', COALESCE(carbon_audit_rollup.carbon_audit_event_count, 0),
                    'latest_event_at', carbon_audit_rollup.latest_carbon_event_at,
                    'evidence_state', CASE
                        WHEN latest_result.id IS NULL THEN 'awaiting_verification'
                        WHEN latest_observation.ndvi IS NULL THEN 'awaiting_satellite_signal'
                        WHEN latest_carbon.metadata->>'skip_reason' IS NOT NULL THEN latest_carbon.metadata->>'skip_reason'
                        ELSE 'ready'
                    END,
                    'methodology_state', COALESCE(
                        latest_carbon.metadata->>'coefficient_status',
                        CAST(NULL::text AS text)
                    ),
                    'calculation_state', COALESCE(latest_carbon.status, CAST(NULL::text AS text)),
                    'last_calculated_at', latest_carbon.created_at::text,
                    'trend_interval_days', latest_carbon.metadata->>'trend_interval_days',
                    'year_over_year', COALESCE(
                        latest_carbon.metadata->'year_over_year',
                        {_stage5_year_over_year_fallback_sql()}
                    ),
                    'coefficient_set_version', latest_carbon.coefficient_set_version,
                    'coefficient_scope', CASE
                        WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%grid_cell:%'
                        THEN 'grid_cell_override'
                        WHEN latest_carbon.metadata->'coefficient_source'->>'applied' LIKE '%region:%'
                        THEN 'region_override'
                        WHEN latest_carbon.id IS NULL THEN NULL
                        ELSE 'global_baseline'
                    END,
                    'coefficient_status', COALESCE(latest_carbon.metadata->>'coefficient_status', CAST(NULL::text AS text)),
                    'calculation_status', COALESCE(latest_carbon.status, CAST(NULL::text AS text))
                ) AS carbon_readiness,
                latest_result.created_at,
                ST_AsGeoJSON(cell.geom, 17)::json AS geometry
            FROM {grid_table} cell
            JOIN region_scope region ON region.id = cell.region_id
            LEFT JOIN latest_result ON latest_result.grid_cell_id = cell.id
            LEFT JOIN {sentinel_table} result_observation
                ON result_observation.id = latest_result.observation_id
            LEFT JOIN latest_observation ON latest_observation.grid_cell_id = cell.id
            LEFT JOIN latest_checks latest_check
                ON latest_check.source_layer = latest_result.metadata->>'ogm_source_layer'
            LEFT JOIN audit_rollup ON audit_rollup.grid_cell_id = cell.id
            LEFT JOIN latest_carbon ON latest_carbon.grid_cell_id = cell.id
            LEFT JOIN carbon_audit_rollup
                ON carbon_audit_rollup.grid_cell_id = cell.id
               AND carbon_audit_rollup.carbon_assessment_id = latest_carbon.id
            WHERE cell.grid_version = :grid_version
              {target_assets_filter}
        )
    """


def _validate_tile(z: int, x: int, y: int) -> None:
    if z < 0 or z > MAX_TILE_ZOOM:
        raise HTTPException(status_code=422, detail="Unsupported tile zoom")
    max_coord = 2**z
    if x < 0 or y < 0 or x >= max_coord or y >= max_coord:
        raise HTTPException(status_code=404, detail="Tile not found")


def _mask_tile_simplify_m(z: int) -> float:
    if z < 6:
        return 1500.0
    if z < 8:
        return 500.0
    if z < 10:
        return 125.0
    if z < MIN_RAW_TILE_ZOOM:
        return 25.0
    return 0.0


def _mask_tile_min_component_area_ha(z: int) -> float:
    for max_zoom, min_area_ha in MASK_MIN_COMPONENT_AREA_BY_ZOOM_HA:
        if z < max_zoom:
            return min_area_ha
    return 0.0


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _encoding_qualities(accept_encoding: str) -> dict[str, float]:
    qualities = {}
    for item in accept_encoding.lower().split(","):
        coding, *parameters = item.split(";")
        coding = coding.strip()
        if not coding:
            continue
        quality = 1.0
        seen_q = False
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name.strip() != "q":
                continue
            value = value.strip()
            if seen_q or not separator or not re.fullmatch(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", value):
                quality = 0.0
                break
            seen_q = True
            quality = float(value)
        # Conflicting duplicate declarations must not defeat an explicit q=0.
        qualities[coding] = min(qualities.get(coding, 1.0), quality)
    return qualities


def _mvt_response(tile, request: Request | None = None):
    content = bytes(tile or b"")
    headers = {
        "Cache-Control": "public, max-age=300",
        "Vary": "Accept-Encoding",
    }
    accept_encoding = ",".join(request.headers.getlist("accept-encoding")) if request is not None else ""
    qualities = _encoding_qualities(accept_encoding)
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    identity_quality = qualities.get("identity", 0.0 if qualities.get("*") == 0 else 1.0)
    if gzip_quality <= 0 and identity_quality <= 0:
        headers["Content-Length"] = "0"
        return Response(status_code=406, headers=headers)
    gzip_preferred = gzip_quality > 0 and gzip_quality >= qualities.get("identity", 0.0)
    if gzip_preferred and (identity_quality <= 0 or len(content) >= MVT_GZIP_MIN_BYTES):
        compressed = gzip.compress(content, compresslevel=3, mtime=0)
        if identity_quality <= 0 or len(compressed) < len(content):
            content = compressed
            headers["Content-Encoding"] = "gzip"
    headers["Content-Length"] = str(len(content))
    return Response(
        content,
        media_type="application/vnd.mapbox-vector-tile",
        headers=headers,
    )
