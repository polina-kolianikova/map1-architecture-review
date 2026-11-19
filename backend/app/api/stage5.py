from __future__ import annotations

import csv
import io
import json
import secrets
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from sqlalchemy import text

from app.api.stage1 import _jsonable
from app.core.config import get_settings
from app.db.session import AppSessionLocal, SessionLocal
from app.services.carbon import carbon_config_hash
from app.services.stage3_partitioned_reads import grid_cells_table_sql
from app.services.stage5_carbon import (
    STAGE5_CARBON_AUDIT_PIPELINE_NAME,
    STAGE5_CARBON_PIPELINE_NAME,
    active_stage5_rule_record,
    load_stage5_effective_config,
    load_stage5_config,
    sanitize_stage5_config_patch,
    stage5_annual_increment_query_params,
    stage5_merge_config,
    stage5_methodology,
    stage5_public_config,
    stage5_scope_params,
    validate_stage5_config,
)


router = APIRouter(prefix="/stage5", tags=["stage5"])
CARBON_EXPORT_FIELDS = (
    "cell_uuid",
    "region_code",
    "grid_version",
    "area_coefficient",
    "anomaly_type",
    "carbon_status",
    "soil_tco2e",
    "biomass_tco2e",
    "root_tco2e",
    "total_potential_tco2e",
    "assessment_year",
    "previous_annual_assessment_id",
    "gross_increment_tco2e_ha_yr",
    "loss_factor_tco2e_ha_yr",
    "net_annual_increment_tco2e_ha_yr",
    "net_annual_increment_tco2e_yr",
    "total_potential_delta_tco2e",
    "net_annual_increment_delta_tco2e_ha_yr",
    "net_annual_increment_delta_tco2e_yr",
    "verification_factor",
    "confidence_score",
    "coefficient_set_version",
    "methodology_version",
    "calibration_source",
    "increment_model",
    "annual_increment_unit",
    "annual_increment_asset_unit",
    "yoy_comparison_state",
    "metric_date",
    "ndvi",
    "ndvi_trend",
    "trend_evidence_state",
)


def _stage5_export_source_ndvi_sql() -> str:
    return """CASE
                WHEN assessment.source_snapshot->>'ndvi' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                THEN (assessment.source_snapshot->>'ndvi')::numeric
            END"""


def _stage5_export_json_numeric_sql(expression: str) -> str:
    return f"""CASE
                WHEN {expression} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                THEN ({expression})::numeric
            END"""


def _stage5_export_current_annual_metadata_sql() -> str:
    return "COALESCE((assessment.metadata->'annual_increment' ? 'soil_increment'), false)"


def _stage5_export_assessment_year_sql() -> str:
    return "(to_jsonb(assessment)->>'assessment_year')::numeric"


def _stage5_export_gross_increment_sql() -> str:
    return "(to_jsonb(assessment)->>'gross_increment_tco2e_ha_yr')::numeric"


def _stage5_export_loss_factor_sql() -> str:
    return "(to_jsonb(assessment)->>'loss_factor_tco2e_ha_yr')::numeric"


def _stage5_export_net_increment_ha_sql() -> str:
    return "(to_jsonb(assessment)->>'net_annual_increment_tco2e_ha_yr')::numeric"


def _stage5_export_area_coefficient_sql() -> str:
    return "(to_jsonb(cell)->>'area_coefficient')::numeric"


def _stage5_export_net_increment_asset_sql() -> str:
    return "(to_jsonb(assessment)->>'net_annual_increment_tco2e_yr')::numeric"


def _require_stage5_rules_write_token(x_stage5_rules_token: str | None = Header(default=None)) -> None:
    raise HTTPException(status_code=501, detail="Private calculation rules and configuration are not distributed.")


@router.get("/status")
def stage5_status(
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = stage5_scope_params(region_code=region_code, grid_version=grid_version)
    with SessionLocal() as db:
        summary = db.execute(_summary_sql(), params).mappings().one_or_none()
        if summary is None:
            raise HTTPException(status_code=503, detail="Stage 5 summary has not been refreshed")
        recent_events = db.execute(_recent_events_sql(grid_table=grid_cells_table_sql(params)), params).mappings().all()
    payload = _jsonable(summary)
    return {"stage": "stage5", "scope": "co2_calculation", "region_code": params["region_code"], "grid_version": params["grid_version"], "methodology_version": payload.get("methodology_version"), "coefficient_set_version": payload.get("coefficient_set_version"), "coefficient_status": payload.get("coefficient_status"), "summary": payload, "methodology": None, "annual_increment": None, "recent_events": [_jsonable(row) for row in recent_events], "calculation_service": "not_distributed"}


@router.get("/rules")
def stage5_rules(
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    raise HTTPException(status_code=501, detail="Private calculation rules and configuration are not distributed.")


@router.post("/rules/validate")
def validate_stage5_rules(payload: dict[str, Any] = Body(...)):
    raise HTTPException(status_code=501, detail="Private calculation rules and configuration are not distributed.")


@router.put("/rules")
def save_stage5_rules(
    payload: dict[str, Any] = Body(...),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    created_by: str = Query("api", min_length=1, max_length=64),
    _write_allowed: None = Depends(_require_stage5_rules_write_token),
):
    raise HTTPException(status_code=501, detail="Private calculation rules and configuration are not distributed.")


@router.post("/rules/reset")
def reset_stage5_rules(
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
    _write_allowed: None = Depends(_require_stage5_rules_write_token),
):
    raise HTTPException(status_code=501, detail="Private calculation rules and configuration are not distributed.")


@router.get("/rules/history")
def stage5_rules_history(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    raise HTTPException(status_code=501, detail="Private calculation rules and configuration are not distributed.")


@router.get("/reports/anomalies")
def stage5_anomaly_report(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    anomaly_type: str = Query("anomaly"),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage5_scope_params(region_code=region_code, grid_version=grid_version),
        **_filter_params(status="all", anomaly_type=anomaly_type),
        "limit": limit,
        "offset": offset,
    }
    with SessionLocal() as db:
        effective_config = None  # Stored values are read without private configuration.
        query_params = {**params, **stage5_annual_increment_query_params(effective_config)}
        rows = db.execute(
            _export_rows_sql(
                include_geometry=False,
                grid_table=grid_cells_table_sql(params),
                status=params["status"],
                anomaly_type=params["anomaly_type"],
            ),
            query_params,
        ).mappings().all()
        total = db.execute(_anomaly_report_total_sql(anomaly_type=params["anomaly_type"]), query_params).scalar_one()
    return {
        "limit": limit,
        "offset": offset,
        "total": int(total or 0),
        "rows": [_jsonable(row) for row in rows],
    }


@router.get("/exports/carbon.csv")
def export_stage5_carbon_csv(
    limit: int = Query(10000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    status: str = Query("all"),
    anomaly_type: str = Query("all"),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage5_scope_params(region_code=region_code, grid_version=grid_version),
        **_filter_params(status=status, anomaly_type=anomaly_type),
        "limit": limit,
        "offset": offset,
    }
    with SessionLocal() as db:
        effective_config = None  # Stored values are read without private configuration.
        rows = db.execute(
            _export_rows_sql(
                include_geometry=False,
                grid_table=grid_cells_table_sql(params),
                status=params["status"],
                anomaly_type=params["anomaly_type"],
            ),
            {**params, **stage5_annual_increment_query_params(effective_config)},
        ).mappings().all()
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=CARBON_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key)) for key in CARBON_EXPORT_FIELDS})
    return Response(
        stream.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="stage5-carbon.csv"'},
    )


@router.get("/exports/carbon.geojson")
def export_stage5_carbon_geojson(
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: str = Query("all"),
    anomaly_type: str = Query("all"),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    params = {
        **stage5_scope_params(region_code=region_code, grid_version=grid_version),
        **_filter_params(status=status, anomaly_type=anomaly_type),
        "limit": limit,
        "offset": offset,
    }
    with SessionLocal() as db:
        effective_config = None  # Stored values are read without private configuration.
        rows = db.execute(
            _export_rows_sql(
                include_geometry=True,
                grid_table=grid_cells_table_sql(params),
                status=params["status"],
                anomaly_type=params["anomaly_type"],
            ),
            {**params, **stage5_annual_increment_query_params(effective_config)},
        ).mappings().all()
    features = []
    for row in rows:
        feature = {
            "type": "Feature",
            "geometry": row["geometry"],
            "properties": {
                key: _json_value(row[key])
                for key in CARBON_EXPORT_FIELDS
                if key in row
            },
        }
        features.append(feature)
    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/exports/carbon.shp.zip")
def export_stage5_carbon_shapefile(
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: str = Query("all"),
    anomaly_type: str = Query("all"),
    region_code: str | None = Query(None),
    grid_version: str | None = Query(None),
):
    try:
        import shapefile
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Shapefile export dependency is not installed") from exc

    params = {
        **stage5_scope_params(region_code=region_code, grid_version=grid_version),
        **_filter_params(status=status, anomaly_type=anomaly_type),
        "limit": limit,
        "offset": offset,
    }
    with SessionLocal() as db:
        effective_config = None  # Stored values are read without private configuration.
        rows = db.execute(
            _export_rows_sql(
                include_geometry=True,
                grid_table=grid_cells_table_sql(params),
                status=params["status"],
                anomaly_type=params["anomaly_type"],
            ),
            {**params, **stage5_annual_increment_query_params(effective_config)},
        ).mappings().all()

    shp = io.BytesIO()
    shx = io.BytesIO()
    dbf = io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    writer.autoBalance = 1
    writer.field("cell_uuid", "C", 36)
    writer.field("region", "C", 48)
    writer.field("grid_ver", "C", 80)
    writer.field("anom", "C", 24)
    writer.field("status", "C", 32)
    writer.field("area_coef", "N", 16, 6)
    writer.field("total", "N", 18, 4)
    writer.field("soil", "N", 18, 4)
    writer.field("biomass", "N", 18, 4)
    writer.field("root", "N", 18, 4)
    writer.field("year", "N", 8, 0)
    writer.field("gross_inc", "N", 18, 4)
    writer.field("loss", "N", 18, 4)
    writer.field("net_ha_yr", "N", 18, 4)
    writer.field("net_yr", "N", 18, 4)
    writer.field("net_d_ha", "N", 18, 4)
    writer.field("net_d_yr", "N", 18, 4)
    writer.field("vf", "N", 12, 4)
    writer.field("conf", "N", 8, 2)
    for row in rows:
        parts = _polygon_parts(row["geometry"])
        if not parts:
            continue
        writer.poly(parts)
        writer.record(
            row["cell_uuid"],
            row["region_code"],
            row["grid_version"],
            row["anomaly_type"],
            row["carbon_status"],
            _float_or_none(row["area_coefficient"]),
            _float_or_none(row["total_potential_tco2e"]),
            _float_or_none(row["soil_tco2e"]),
            _float_or_none(row["biomass_tco2e"]),
            _float_or_none(row["root_tco2e"]),
            int(row["assessment_year"]) if row["assessment_year"] is not None else None,
            _float_or_none(row["gross_increment_tco2e_ha_yr"]),
            _float_or_none(row["loss_factor_tco2e_ha_yr"]),
            _float_or_none(row["net_annual_increment_tco2e_ha_yr"]),
            _float_or_none(row["net_annual_increment_tco2e_yr"]),
            _float_or_none(row["net_annual_increment_delta_tco2e_ha_yr"]),
            _float_or_none(row["net_annual_increment_delta_tco2e_yr"]),
            _float_or_none(row["verification_factor"]),
            _float_or_none(row["confidence_score"]),
        )
    writer.close()

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("stage5-carbon.shp", shp.getvalue())
        output.writestr("stage5-carbon.shx", shx.getvalue())
        output.writestr("stage5-carbon.dbf", dbf.getvalue())
        output.writestr("stage5-carbon.prj", _wgs84_prj())
        output.writestr("stage5-carbon.cpg", "UTF-8")
    return Response(
        archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="stage5-carbon-shapefile.zip"'},
    )


def _filter_params(*, status: str, anomaly_type: str) -> dict[str, object]:
    status = status.strip().lower()
    anomaly_type = anomaly_type.strip().lower()
    if status not in {"all", "calculated", "skipped_missing_evidence", "pending_coefficients", "failed"}:
        raise HTTPException(status_code=422, detail="Unsupported carbon status")
    if anomaly_type not in {"all", "ok", "watch", "anomaly", "insufficient_data", "unverified"}:
        raise HTTPException(status_code=422, detail="Unsupported anomaly type")
    return {
        "status": status,
        "anomaly_type": anomaly_type,
    }


def _summary_sql():
    return text(
        f"""
        SELECT
            total_cells,
            carbon_assessments,
            calculated_cells,
            skipped_missing_evidence_cells,
            baseline_cells,
            historical_trend_cells,
            current_observation_only_cells,
            ok_cells,
            watch_cells,
            anomaly_cells,
            insufficient_data_cells,
            unverified_cells,
            total_potential_tco2e,
            net_annual_increment_tco2e_yr,
            average_net_annual_increment_tco2e_ha_yr,
            average_confidence_score,
            latest_calculated_at::text AS latest_calculated_at,
            refreshed_at::text AS refreshed_at,
            source_layer_b_run_id,
            metadata
        FROM stage5_carbon_summaries
        WHERE region_code = :region_code
          AND grid_version = :grid_version
          AND pipeline = '{STAGE5_CARBON_PIPELINE_NAME}'
        ORDER BY refreshed_at DESC, id DESC
        LIMIT 1
        """
    )


def _summary_recalculation_state(summary: dict[str, Any], effective_config_hash: str) -> dict[str, Any]:
    metadata = summary.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    summary_config_hash = metadata.get("config_hash") if isinstance(metadata, dict) else None
    is_current = bool(summary_config_hash and secrets.compare_digest(str(summary_config_hash), effective_config_hash))
    return {
        "recalculation_required": not is_current,
        "summary_state": "current" if is_current else "stale_summary",
    }


def _rules_history_sql():
    return text(
        """
        SELECT
            id,
            region_code,
            grid_version,
            config_patch,
            effective_config_hash,
            status,
            created_by,
            metadata,
            created_at,
            retired_at
        FROM stage5_rule_configs
        WHERE region_code = :region_code
          AND grid_version = :grid_version
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
        OFFSET :offset
        """
    )


def _recent_events_sql(*, grid_table: str = "grid_cells"):
    return text(
        f"""
        WITH region_scope AS (
            SELECT id
            FROM regions
            WHERE code = :region_code
        )
        SELECT
            event.event_type,
            event.previous_status,
            event.new_status,
            event.coefficient_set_version,
            event.created_at,
            event.metadata->>'anomaly_type' AS anomaly_type
        FROM carbon_assessment_events event
        JOIN {grid_table} cell ON cell.id = event.grid_cell_id
        WHERE event.metadata->>'pipeline' = '{STAGE5_CARBON_AUDIT_PIPELINE_NAME}'
          AND cell.region_id = (SELECT id FROM region_scope)
          AND cell.grid_version = :grid_version
        ORDER BY event.created_at DESC, event.id DESC
        LIMIT 20
        """
    )


def _export_count_sql(
    *, status: str, anomaly_type: str, grid_table: str = "grid_cells"
):
    filters = _export_filter_sql(status=status, anomaly_type=anomaly_type)
    return text(
        f"""
        WITH region_scope AS (
            SELECT id
            FROM regions
            WHERE code = :region_code
        )
        SELECT count(*)::bigint
        FROM carbon_assessments assessment
        JOIN {grid_table} cell ON cell.id = assessment.grid_cell_id
        WHERE assessment.is_current
          AND assessment.metadata->>'pipeline' = '{STAGE5_CARBON_PIPELINE_NAME}'
          AND cell.region_id = (SELECT id FROM region_scope)
          AND cell.grid_version = :grid_version
          {filters}
        """
    )


def _anomaly_report_total_sql(*, anomaly_type: str):
    if anomaly_type == "all":
        column = "carbon_assessments"
    elif anomaly_type == "ok":
        column = "ok_cells"
    elif anomaly_type == "watch":
        column = "watch_cells"
    elif anomaly_type == "anomaly":
        column = "anomaly_cells"
    elif anomaly_type == "insufficient_data":
        column = "insufficient_data_cells"
    elif anomaly_type == "unverified":
        column = "unverified_cells"
    else:
        column = "0"
    return text(
        f"""
        SELECT COALESCE({column}, 0)::bigint
        FROM stage5_carbon_summaries
        WHERE region_code = :region_code
          AND grid_version = :grid_version
          AND pipeline = '{STAGE5_CARBON_PIPELINE_NAME}'
        ORDER BY refreshed_at DESC, id DESC
        LIMIT 1
        """
    )


def _export_rows_sql(
    *,
    include_geometry: bool,
    status: str = "all",
    anomaly_type: str = "all",
    grid_table: str = "grid_cells",
):
    geometry_column = "ST_AsGeoJSON(cell.geom)::json AS geometry," if include_geometry else ""
    filters = _export_filter_sql(status=status, anomaly_type=anomaly_type)
    return text(
        f"""
        WITH region_scope AS (
            SELECT id, code
            FROM regions
            WHERE code = :region_code
        )
        SELECT
            cell.uuid::text AS cell_uuid,
            region.code AS region_code,
            cell.grid_version,
            cell.area_coefficient AS area_coefficient,
            assessment.metadata->>'anomaly_type' AS anomaly_type,
            assessment.status AS carbon_status,
            assessment.soil_tco2e,
            assessment.biomass_tco2e,
            assessment.root_tco2e,
            assessment.total_potential_tco2e,
            {_stage5_export_assessment_year_sql()} AS assessment_year,
            assessment.previous_annual_assessment_id,
            {_stage5_export_gross_increment_sql()} AS gross_increment_tco2e_ha_yr,
            {_stage5_export_loss_factor_sql()} AS loss_factor_tco2e_ha_yr,
            {_stage5_export_net_increment_ha_sql()} AS net_annual_increment_tco2e_ha_yr,
            {_stage5_export_net_increment_asset_sql()} AS net_annual_increment_tco2e_yr,
            assessment.total_potential_delta_tco2e,
            assessment.net_annual_increment_delta_tco2e_ha_yr,
            assessment.net_annual_increment_delta_tco2e_yr,
            assessment.verification_factor,
            assessment.confidence_score,
            assessment.coefficient_set_version,
            assessment.methodology_version,
            assessment.calibration_source,
            COALESCE(
                assessment.metadata->>'increment_model',
                'baseline-ipcc-ogm-v0.1'
            ) AS increment_model,
            COALESCE(
                assessment.metadata->'annual_increment'->>'unit',
                'tCO2e/ha/year'
            ) AS annual_increment_unit,
            COALESCE(
                assessment.metadata->'annual_increment'->>'asset_unit',
                'tCO2e/year'
            ) AS annual_increment_asset_unit,
            COALESCE(
                assessment.metadata->'year_over_year'->>'comparison_state',
                assessment.source_snapshot->>'year_over_year_comparison_state',
                CASE
                    WHEN {_stage5_export_assessment_year_sql()} IS NULL THEN 'awaiting_current_year'
                    WHEN assessment.previous_annual_assessment_id IS NULL
                    THEN 'awaiting_previous_carbon_snapshot'
                    ELSE 'annual_baseline'
                END
            ) AS yoy_comparison_state,
            assessment.source_snapshot->>'metric_date' AS metric_date,
            {_stage5_export_source_ndvi_sql()} AS ndvi,
            CASE
                WHEN assessment.source_snapshot->>'ndvi_trend' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                THEN (assessment.source_snapshot->>'ndvi_trend')::numeric
            END AS ndvi_trend,
            assessment.source_snapshot->>'trend_evidence_state' AS trend_evidence_state,
            {geometry_column}
            assessment.created_at
        FROM carbon_assessments assessment
        JOIN {grid_table} cell ON cell.id = assessment.grid_cell_id
        JOIN region_scope region ON region.id = cell.region_id
        WHERE assessment.is_current
          AND assessment.metadata->>'pipeline' = '{STAGE5_CARBON_PIPELINE_NAME}'
          AND cell.grid_version = :grid_version
          {filters}
        ORDER BY assessment.created_at DESC, assessment.id DESC
        LIMIT :limit
        OFFSET :offset
        """
    )


def _export_filter_sql(*, status: str, anomaly_type: str) -> str:
    filters = []
    if status != "all":
        filters.append("AND assessment.status = :status")
    if anomaly_type != "all":
        filters.append("AND assessment.metadata->>'anomaly_type' = :anomaly_type")
    return "\n          ".join(filters)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    prepared = _json_value(value)
    if isinstance(prepared, (dict, list)):
        return json.dumps(prepared, ensure_ascii=True, sort_keys=True)
    return str(prepared)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            return int(normalized)
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _jsonb_record(row: Any) -> dict[str, Any]:
    record = dict(row)
    for key in ("config_patch", "metadata"):
        value = record.get(key)
        if isinstance(value, str):
            record[key] = json.loads(value)
    return record


def _public_rule_record(row: Any) -> dict[str, Any]:
    record = _jsonb_record(row)
    return _jsonable(
        {
            "id": record.get("id"),
            "region_code": record.get("region_code"),
            "grid_version": record.get("grid_version"),
            "status": record.get("status"),
            "created_by": record.get("created_by"),
            "created_at": record.get("created_at"),
            "retired_at": record.get("retired_at"),
        }
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _polygon_parts(geometry: dict[str, Any] | str | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    if isinstance(geometry, str):
        geometry = json.loads(geometry)
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        return [_ring_points(ring) for ring in coordinates if ring]
    if geometry_type == "MultiPolygon":
        parts = []
        for polygon in coordinates:
            parts.extend(_ring_points(ring) for ring in polygon if ring)
        return parts
    return []


def _ring_points(ring: list[list[float]]) -> list[list[float]]:
    return [[float(point[0]), float(point[1])] for point in ring if len(point) >= 2]


def _wgs84_prj() -> str:
    return (
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
    )
