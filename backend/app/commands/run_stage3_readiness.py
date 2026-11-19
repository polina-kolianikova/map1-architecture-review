from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.api.stage1 import stage1_params
from app.commands.check_stage3_forest_clip import forest_clip_report
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.forest_scope import (
    FILTERED_FOREST_REGION_SOURCE,
    FOREST_REGION_SOURCE,
    normalize_region_metadata,
    public_forest_mask_metadata_ready,
)
from app.services.source_strategy import stage1_source_strategy
from app.services.stage3_public_v9 import stage3_public_v9_manifest_hash


EXPECTED_TABLES = (
    "grid_cells",
    "ogm_parcels",
    "sentinel_observations",
    "verification_results",
    "verification_audit_events",
    "carbon_assessments",
    "carbon_assessment_events",
    "stage3_grid_generation_runs",
    "stage3_grid_generation_tiles",
    "stage3_layer_a_ingest_runs",
    "stage3_layer_a_ingest_tiles",
)

EXPECTED_INDEXES = (
    "regions_geom_gix",
    "grid_cells_geom_gix",
    "grid_cells_region_version_uuid_idx",
    "ogm_parcels_geom_gix",
    "ogm_parcels_current_region_layer_idx",
    "sentinel_observations_stage_region_grid_cell_date_idx",
    "sentinel_observations_scope_grid_cell_date_idx",
    "verification_results_stage_region_grid_cell_idx",
    "verification_results_scope_grid_cell_idx",
    "verification_results_scope_status_idx",
    "verification_audit_events_stage_region_grid_cell_idx",
    "verification_audit_events_scope_cell_created_idx",
    "carbon_assessments_current_status_cell_idx",
    "carbon_assessments_provenance_idx",
    "carbon_assessment_events_cell_created_idx",
    "carbon_assessment_events_audit_assessment_cell_idx",
    "stage3_grid_generation_runs_scope_idx",
    "stage3_grid_generation_tiles_status_idx",
    "stage3_layer_a_ingest_runs_scope_idx",
    "stage3_layer_a_ingest_tiles_status_idx",
    "regions_metadata_gin",
)

PARTITION_TARGETS = (
    "grid_cells",
    "sentinel_observations",
    "verification_results",
)

PUBLIC_CONTRACT_ACCEPTANCE_MIN_CELLS = 1_000_000

REQUIRED_PERFORMANCE_PROBES = {
    "asset_register_page",
    "asset_register_count",
    "latest_ndvi_history",
    "layer_a_intersections",
    "raw_grid_tile_window",
    "layer_b_tile_window",
    "layer_c_tile_window",
    "raw_grid_mvt_tile",
    "layer_b_mvt_tile",
    "layer_c_mvt_tile",
    "carbon_asset_page",
    "carbon_audit_tail",
}

PUBLIC_REQUIRED_PERFORMANCE_PROBES = {
    "asset_register_page",
    "asset_register_count",
    "layer_a_intersections",
    "raw_grid_tile_window",
    "raw_grid_mvt_tile",
}

LAYER_A_RUN_SOURCE_BY_SOURCE_LAYER = {
    "ogm_esatis_bolme": "bolme",
    "ogm_yayin_mescere_wms_svg": "mescere",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Report Stage 3 readiness for the active workspace")
    parser.add_argument("--region-code", default="")
    parser.add_argument("--grid-version", default="")
    parser.add_argument("--acceptance-scope", choices=("public-ogm", "full-scale"), default="public-ogm")
    parser.add_argument("--expected-grid-cells", type=int, default=0)
    parser.add_argument("--expected-layer-a-features", type=int, default=0)
    parser.add_argument("--max-source-age-days", type=int, default=7)
    parser.add_argument("--trend-months", type=int, default=12)
    parser.add_argument("--min-ndvi-trend-coverage", type=float, default=0.9)
    parser.add_argument("--performance-report", default="")
    parser.add_argument("--forest-clip-report", default="")
    parser.add_argument("--grid-mask-proof-report", default="")
    parser.add_argument("--clean-mask-integrity-report", default="")
    parser.add_argument("--ogm-source-agreement-report", default="")
    parser.add_argument("--osm-source-agreement-report", default="")
    parser.add_argument("--grid-spatial-integrity-report", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--fail-on-not-ready", action="store_true")
    args = parser.parse_args()
    if args.trend_months <= 0:
        raise ValueError("trend-months must be greater than zero")
    if args.min_ndvi_trend_coverage < 0 or args.min_ndvi_trend_coverage > 1:
        raise ValueError("min-ndvi-trend-coverage must be between 0 and 1")
    started = time.perf_counter()
    with SessionLocal() as db:
        params = stage1_params(region_code=args.region_code or None, grid_version=args.grid_version or None, db=db)
        params["forest_mask_source_layers"] = params.get("forest_mask_source_layers") or params["layer_a_source_layers"]
        params["auxiliary_forest_mask_source_layers"] = ["osm_forest_mask"]
        params["auxiliary_nonforest_exclusion_source_layers"] = [
            "osm_nonforest_exclusion",
            "esa_worldcover_nonforest_exclusion",
        ]
        params["trend_months"] = args.trend_months
        region_metadata_column_ready = _region_metadata_column_ready(db)
        schema = _schema_status(
            table_rows=[dict(row) for row in db.execute(_table_sql(), {"names": list(EXPECTED_TABLES)}).mappings()],
            index_rows=[dict(row) for row in db.execute(_index_sql(), {"names": list(EXPECTED_INDEXES)}).mappings()],
            partition_rows=[
                dict(row)
                for row in db.execute(_partition_sql(), {"names": list(PARTITION_TARGETS)}).mappings()
            ],
            shadow_partition_rows=[
                dict(row)
                for row in db.execute(_shadow_partition_sql(), {"names": list(PARTITION_TARGETS)}).mappings()
            ],
            shadow_partition_counts_match=_shadow_partition_validation(db, params["region_code"]),
        )
        schema["region_metadata_column_ready"] = region_metadata_column_ready
        if region_metadata_column_ready:
            summary = _summary(db, params)
            source_rows = [dict(row) for row in db.execute(_source_sql(), params).mappings()]
            query_probe = dict(db.execute(_query_probe_sql(), params).mappings().one())
            grid_runs = (
                [dict(row) for row in db.execute(_grid_run_sql(), params).mappings()]
                if _tracking_tables_ready(schema, "stage3_grid_generation_runs", "stage3_grid_generation_tiles")
                else []
            )
            layer_a_runs = (
                [dict(row) for row in db.execute(_layer_a_run_sql(), params).mappings()]
                if _tracking_tables_ready(schema, "stage3_layer_a_ingest_runs", "stage3_layer_a_ingest_tiles")
                else []
            )
            forest_clip = _load_forest_clip_report(
                args.forest_clip_report,
                {"region_code": params["region_code"], "grid_version": params["grid_version"]},
            ) or forest_clip_report(
                db,
                region_code=params["region_code"],
                grid_version=params["grid_version"],
            )
        else:
            summary = _empty_summary()
            source_rows = []
            query_probe = _empty_query_probe()
            grid_runs = []
            layer_a_runs = []
            forest_clip = _empty_forest_clip(params["region_code"], params["grid_version"])
    expected_scope = {
        "region_code": params["region_code"],
        "grid_version": params["grid_version"],
    }
    settings = get_settings()
    performance_report = _load_performance_report(
        args.performance_report,
        expected_scope,
        {
            "grid_cells": summary.get("grid_cells"),
            "layer_a_features": summary.get("layer_a_features"),
            "ndvi_observations": summary.get("ndvi_observations"),
            "verification_results": summary.get("verification_results"),
            "carbon_assessments": summary.get("carbon_assessments"),
            "carbon_audit_events": summary.get("carbon_audit_events"),
        },
        required_probe_names=_required_performance_probes(args.acceptance_scope),
    )
    strict_proof_reports = {
        "grid_mask_proof": _load_ready_report(
            args.grid_mask_proof_report,
            {"region_code": params["region_code"], "grid_version": params["grid_version"]},
            "grid mask proof",
        ),
        "mask_formula_integrity": _load_ready_report(
            args.clean_mask_integrity_report,
            {
                "region_code": params["region_code"],
                "mask_version": params["region_code"],
                "formula_manifest_hash": stage3_public_v9_manifest_hash(),
            },
            "mask formula integrity",
        ),
        "grid_spatial_integrity": _load_ready_report(
            args.grid_spatial_integrity_report,
            {"region_code": params["region_code"], "grid_version": params["grid_version"]},
            "grid spatial integrity",
        ),
    }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    report = {
        "stage": "stage3",
        "scope": {
            "region_code": params["region_code"],
            "grid_version": params["grid_version"],
            "acceptance_scope": args.acceptance_scope,
            "expected_grid_cells": args.expected_grid_cells or None,
            "expected_layer_a_features": args.expected_layer_a_features or None,
            "max_source_age_days": args.max_source_age_days,
            "trend_months": args.trend_months,
            "min_ndvi_trend_coverage": args.min_ndvi_trend_coverage,
            "official_full_layer_a_status": "backlog" if args.acceptance_scope == "public-ogm" else "required",
        },
        "elapsed_ms": elapsed_ms,
        "summary": summary,
        "layer_a_sources": source_rows,
        "query_probe": query_probe,
        "schema": schema,
        "grid_generation_runs": grid_runs,
        "layer_a_ingest_runs": layer_a_runs,
        "forest_clip_report": forest_clip,
        "strict_proof_reports": strict_proof_reports,
        "performance_report": performance_report,
        "operational": {
            "ogm_tls_verify": settings.ogm_tls_verify,
        },
        "readiness": _readiness(
            summary,
            source_rows,
            schema,
            args.expected_grid_cells,
            args.expected_layer_a_features,
            grid_runs,
            layer_a_runs,
            params["layer_a_source_layers"] + params["auxiliary_forest_mask_source_layers"],
            performance_report.get("ready", False),
            args.max_source_age_days,
            args.min_ndvi_trend_coverage,
            args.acceptance_scope,
            forest_clip,
            strict_proof_reports,
        ),
    }
    report["contract_checklist"] = _contract_checklist(report)
    report["stage3_acceptance_summary"] = _stage3_acceptance_summary(report)
    payload = json.dumps(report, indent=2, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if args.fail_on_not_ready and not report["contract_checklist"]["ready_for_contract_acceptance"]:
        raise SystemExit(f"Stage 3 {args.acceptance_scope} readiness checks did not pass")


def _summary_sql():
    return text(
        """
        WITH source_scope AS (
            SELECT
                region.id AS region_id,
                COALESCE(
                    NULLIF(region.metadata->'forest_mask'->>'source_region_code', ''),
                    region.code
                ) AS layer_a_region_code
            FROM regions region
            WHERE region.code = :region_code
        )
        SELECT
            (
                SELECT region.metadata
                FROM regions region
                WHERE region.code = :region_code
            ) AS region_metadata,
            (
                SELECT COALESCE(region.metadata->>'forest_only', 'false') = 'true'
                FROM regions region
                WHERE region.code = :region_code
            ) AS forest_only_region,
            (
                SELECT region.metadata->>'stage3_region_source'
                FROM regions region
                WHERE region.code = :region_code
            ) AS region_source,
            (
                SELECT COALESCE(region.metadata->'forest_mask'->>'source', '')
                FROM regions region
                WHERE region.code = :region_code
            ) AS forest_mask_source,
            (
                SELECT COALESCE(region.metadata->'forest_mask'->'source_layers', '[]'::jsonb)
                FROM regions region
                WHERE region.code = :region_code
            ) AS forest_mask_source_layers,
            (
                SELECT layer_a_region_code
                FROM source_scope
            ) AS layer_a_region_code,
            (
                SELECT count(*)
                FROM grid_cells cell
                WHERE cell.region_id = (
                      SELECT region_id
                      FROM source_scope
                  )
                  AND cell.grid_version = :grid_version
            ) AS grid_cells,
            (
                SELECT count(*)
                FROM ogm_parcels parcel
                WHERE parcel.is_current
                  AND parcel.source_layer = ANY(:layer_a_source_layers)
                  AND parcel.attributes->>'region_code' = (
                      SELECT layer_a_region_code
                      FROM source_scope
                  )
            ) AS layer_a_features,
            (
                SELECT count(*)
                FROM verification_results result
                WHERE result.is_current
                  AND result.metadata->>'pipeline' = 'stage1_verification'
                  AND result.region_id = (
                      SELECT region_id
                      FROM source_scope
                  )
                  AND result.region_code = :region_code
                  AND result.grid_version = :grid_version
            ) AS verification_results,
            (
                SELECT count(*)
                FROM verification_results result
                WHERE result.is_current
                  AND result.metadata->>'pipeline' = 'stage1_verification'
                  AND result.region_id = (
                      SELECT region_id
                      FROM source_scope
                  )
                  AND result.region_code = :region_code
                  AND result.grid_version = :grid_version
                  AND result.metadata->>'ogm_source_layer' IS NOT NULL
                  AND NOT (result.metadata->>'ogm_source_layer' = ANY(:layer_a_source_layers))
            ) AS verification_unexpected_layer_a_sources,
            (
                SELECT count(*)
                FROM verification_results result
                WHERE result.is_current
                  AND result.metadata->>'pipeline' = 'stage1_verification'
                  AND result.region_id = (
                      SELECT region_id
                      FROM source_scope
                  )
                  AND result.region_code = :region_code
                  AND result.grid_version = :grid_version
                  AND NOT (
                      COALESCE(result.metadata->'verification_source_layers', '[]'::jsonb)
                      ?& CAST(:layer_a_source_layers AS text[])
                  )
            ) AS verification_source_layer_mismatches,
            (
                SELECT count(*)
                FROM sentinel_observations observation
                JOIN grid_cells cell ON cell.id = observation.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                  AND observation.is_current
                  AND observation.ndvi IS NOT NULL
                  AND COALESCE(observation.metadata->>'status', CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
                  AND observation.region_code = :region_code
                  AND observation.grid_version = :grid_version
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS ndvi_observations,
            (
                SELECT max(observation.acquisition_date)::text
                FROM sentinel_observations observation
                JOIN grid_cells cell ON cell.id = observation.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                  AND observation.is_current
                  AND observation.ndvi IS NOT NULL
                  AND COALESCE(observation.metadata->>'status', CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
                  AND observation.region_code = :region_code
                  AND observation.grid_version = :grid_version
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS latest_ndvi_date,
            (
                SELECT count(*)
                FROM (
                    SELECT observation.grid_cell_id
                    FROM sentinel_observations observation
                    JOIN grid_cells cell ON cell.id = observation.grid_cell_id
                    JOIN regions region ON region.id = cell.region_id
                    WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                      AND observation.ndvi IS NOT NULL
                      AND COALESCE(observation.metadata->>'status', CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
                      AND observation.metadata->>'statistics_source' = 'sentinel_hub_statistical_api'
                      AND observation.region_code = :region_code
                      AND observation.grid_version = :grid_version
                      AND region.code = :region_code
                      AND cell.grid_version = :grid_version
                      AND observation.acquisition_date >= (
                          SELECT COALESCE(max(anchor.acquisition_date), CURRENT_DATE)
                          FROM sentinel_observations anchor
                          JOIN grid_cells anchor_cell ON anchor_cell.id = anchor.grid_cell_id
                          JOIN regions anchor_region ON anchor_region.id = anchor_cell.region_id
                          WHERE anchor.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                            AND anchor.region_code = :region_code
                            AND anchor.grid_version = :grid_version
                            AND anchor.ndvi IS NOT NULL
                            AND COALESCE(anchor.metadata->>'status', CASE WHEN anchor.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
                            AND anchor_region.code = :region_code
                            AND anchor_cell.grid_version = :grid_version
                      ) - (:trend_months * INTERVAL '1 month')
                    GROUP BY observation.grid_cell_id
                    HAVING count(DISTINCT observation.acquisition_date::date) >= 2
                ) trend_cells
            ) AS ndvi_trend_cells,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS calculated_carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND assessment.status = 'skipped_missing_evidence'
                  AND assessment.metadata->>'skip_reason' IS NOT NULL
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS skipped_missing_evidence_carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'coefficient_status' IN ('baseline', 'approved')
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS baseline_calculated_carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'trend_evidence_state' = 'historical_baseline'
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS historical_trend_carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'calculation_area_basis' = 'layer_a_intersection_area'
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS forest_area_carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'coefficient_status' = 'approved'
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
            ) AS approved_calculated_carbon_assessments
            ,
            (
                SELECT count(*)
                FROM carbon_assessment_events event
                JOIN grid_cells cell ON cell.id = event.grid_cell_id
                  JOIN regions region ON region.id = cell.region_id
                  WHERE region.code = :region_code
                  AND event.metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
                  AND cell.grid_version = :grid_version
            ) AS carbon_audit_events
            ,
            (
                SELECT count(*)
                FROM carbon_assessments assessment
                JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                JOIN regions region ON region.id = cell.region_id
                WHERE assessment.is_current
                  AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                  AND region.code = :region_code
                  AND cell.grid_version = :grid_version
                  AND NOT EXISTS (
                      SELECT 1
                      FROM carbon_assessment_events event
                      WHERE event.carbon_assessment_id = assessment.id
                        AND event.grid_cell_id = assessment.grid_cell_id
                        AND event.metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
                  )
            ) AS carbon_assessments_without_audit
        """
    )


def _summary(db, params: dict) -> dict:
    summary = dict(db.execute(_summary_base_sql(), params).mappings().one())
    summary.update(dict(db.execute(_summary_verification_sql(), params).mappings().one()))
    summary.update(dict(db.execute(_summary_ndvi_sql(), params).mappings().one()))
    summary.update(dict(db.execute(_summary_ndvi_trend_sql(), params).mappings().one()))
    summary.update(dict(db.execute(_summary_carbon_sql(), params).mappings().one()))
    summary.update(dict(db.execute(_summary_carbon_audit_sql(), params).mappings().one()))
    summary.update(dict(db.execute(_summary_carbon_without_audit_sql(), params).mappings().one()))
    return summary


def _summary_base_sql():
    return text(
        """
        WITH source_scope AS (
            SELECT
                region.id AS region_id,
                region.metadata AS region_metadata,
                COALESCE(region.metadata->>'forest_only', 'false') = 'true' AS forest_only_region,
                region.metadata->>'stage3_region_source' AS region_source,
                COALESCE(region.metadata->'forest_mask'->>'source', '') AS forest_mask_source,
                COALESCE(region.metadata->'forest_mask'->'source_layers', '[]'::jsonb) AS forest_mask_source_layers,
                COALESCE(
                    NULLIF(region.metadata->'forest_mask'->>'source_region_code', ''),
                    region.code
                ) AS layer_a_region_code
            FROM regions region
            WHERE region.code = :region_code
        )
        SELECT
            source_scope.region_metadata,
            source_scope.forest_only_region,
            source_scope.region_source,
            source_scope.forest_mask_source,
            source_scope.forest_mask_source_layers,
            source_scope.layer_a_region_code,
            (
                SELECT count(*)
                FROM grid_cells cell
                WHERE cell.region_id = source_scope.region_id
                  AND cell.grid_version = :grid_version
            ) AS grid_cells,
            (
                SELECT count(*)
                FROM ogm_parcels parcel
                WHERE parcel.is_current
                  AND parcel.source_layer = ANY(:layer_a_source_layers)
                  AND parcel.attributes->>'region_code' = source_scope.layer_a_region_code
            ) AS layer_a_features
        FROM source_scope
        """
    )


def _summary_verification_sql():
    return text(
        """
        SELECT
            count(*) AS verification_results,
            count(*) FILTER (
                WHERE result.metadata->>'ogm_source_layer' IS NOT NULL
                  AND NOT (result.metadata->>'ogm_source_layer' = ANY(:layer_a_source_layers))
            ) AS verification_unexpected_layer_a_sources,
            count(*) FILTER (
                WHERE NOT (
                    COALESCE(result.metadata->'verification_source_layers', '[]'::jsonb)
                    ?& CAST(:layer_a_source_layers AS text[])
                )
            ) AS verification_source_layer_mismatches
        FROM verification_results result
        WHERE result.is_current
          AND result.metadata->>'pipeline' = 'stage1_verification'
          AND result.region_id = (
              SELECT region.id
              FROM regions region
              WHERE region.code = :region_code
          )
          AND result.region_code = :region_code
          AND result.grid_version = :grid_version
        """
    )


def _summary_ndvi_sql():
    return text(
        """
        SELECT
            count(*) AS ndvi_observations,
            max(observation.acquisition_date)::text AS latest_ndvi_date
        FROM sentinel_observations observation
        WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
          AND observation.is_current
          AND observation.ndvi IS NOT NULL
          AND COALESCE(observation.metadata->>'status', CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
          AND observation.region_code = :region_code
          AND observation.grid_version = :grid_version
        """
    )


def _summary_ndvi_trend_sql():
    return text(
        """
        SELECT count(*) AS ndvi_trend_cells
        FROM (
            SELECT observation.grid_cell_id
            FROM sentinel_observations observation
            WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
              AND observation.ndvi IS NOT NULL
              AND COALESCE(observation.metadata->>'status', CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
              AND observation.metadata->>'statistics_source' = 'sentinel_hub_statistical_api'
              AND observation.region_code = :region_code
              AND observation.grid_version = :grid_version
              AND observation.acquisition_date >= (
                  SELECT COALESCE(max(anchor.acquisition_date), CURRENT_DATE)
                  FROM sentinel_observations anchor
                  WHERE anchor.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                    AND anchor.region_code = :region_code
                    AND anchor.grid_version = :grid_version
                    AND anchor.ndvi IS NOT NULL
                    AND COALESCE(anchor.metadata->>'status', CASE WHEN anchor.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
              ) - (:trend_months * INTERVAL '1 month')
            GROUP BY observation.grid_cell_id
            HAVING count(DISTINCT observation.acquisition_date::date) >= 2
        ) trend_cells
        """
    )


def _summary_carbon_sql():
    return text(
        """
        SELECT
            count(*) AS carbon_assessments,
            count(*) FILTER (
                WHERE assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
            ) AS calculated_carbon_assessments,
            count(*) FILTER (
                WHERE assessment.status = 'skipped_missing_evidence'
                  AND assessment.metadata->>'skip_reason' IS NOT NULL
            ) AS skipped_missing_evidence_carbon_assessments,
            count(*) FILTER (
                WHERE assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'coefficient_status' IN ('baseline', 'approved')
            ) AS baseline_calculated_carbon_assessments,
            count(*) FILTER (
                WHERE assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'trend_evidence_state' = 'historical_baseline'
            ) AS historical_trend_carbon_assessments,
            count(*) FILTER (
                WHERE assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'calculation_area_basis' = 'layer_a_intersection_area'
            ) AS forest_area_carbon_assessments,
            count(*) FILTER (
                WHERE assessment.status = 'calculated'
                  AND assessment.total_potential_tco2e IS NOT NULL
                  AND assessment.metadata->>'coefficient_status' = 'approved'
            ) AS approved_calculated_carbon_assessments
        FROM carbon_assessments assessment
        JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
        WHERE assessment.is_current
          AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
          AND cell.region_id = (
              SELECT region.id
              FROM regions region
              WHERE region.code = :region_code
          )
          AND cell.grid_version = :grid_version
        """
    )


def _summary_carbon_audit_sql():
    return text(
        """
        SELECT count(*) AS carbon_audit_events
        FROM carbon_assessment_events event
        JOIN grid_cells cell ON cell.id = event.grid_cell_id
        WHERE event.metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
          AND cell.region_id = (
              SELECT region.id
              FROM regions region
              WHERE region.code = :region_code
          )
          AND cell.grid_version = :grid_version
        """
    )


def _summary_carbon_without_audit_sql():
    return text(
        """
        SELECT count(*) AS carbon_assessments_without_audit
        FROM carbon_assessments assessment
        JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
        LEFT JOIN LATERAL (
            SELECT 1
            FROM carbon_assessment_events event
            WHERE event.carbon_assessment_id = assessment.id
              AND event.grid_cell_id = assessment.grid_cell_id
              AND event.metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
            LIMIT 1
        ) audit_event ON true
        WHERE assessment.is_current
          AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
          AND cell.region_id = (
              SELECT region.id
              FROM regions region
              WHERE region.code = :region_code
          )
          AND cell.grid_version = :grid_version
          AND audit_event IS NULL
        """
    )


def _region_metadata_column_ready(db) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'regions'
                      AND column_name = 'metadata'
                )
                """
            )
        ).scalar_one()
    )


def _empty_summary() -> dict:
    return {
        "region_metadata": {},
        "forest_only_region": False,
        "region_source": "",
        "forest_mask_source": "",
        "forest_mask_source_layers": [],
        "grid_cells": 0,
        "layer_a_features": 0,
        "verification_results": 0,
        "verification_unexpected_layer_a_sources": 0,
        "verification_source_layer_mismatches": 0,
        "ndvi_observations": 0,
        "latest_ndvi_date": None,
        "ndvi_trend_cells": 0,
        "carbon_assessments": 0,
        "calculated_carbon_assessments": 0,
        "skipped_missing_evidence_carbon_assessments": 0,
        "baseline_calculated_carbon_assessments": 0,
        "historical_trend_carbon_assessments": 0,
        "forest_area_carbon_assessments": 0,
        "approved_calculated_carbon_assessments": 0,
        "carbon_audit_events": 0,
        "carbon_assessments_without_audit": 0,
    }


def _empty_query_probe() -> dict:
    return {
        "anomaly_cells": 0,
        "watch_cells": 0,
        "ok_cells": 0,
        "latest_verification_at": None,
    }


def _empty_forest_clip(region_code: str, grid_version: str) -> dict:
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "cell_count": 0,
        "invalid_cells": 0,
        "cells_outside_forest_mask": 0,
        "non_hectare_cells": 0,
        "forest_mask_coverage_ratio": 0,
        "forest_mask_source": "",
        "forest_mask_method": "",
        "checks": {
            "ready": False,
            "has_cells": False,
            "valid_geometry": False,
            "all_cells_inside_forest_mask": False,
            "full_hectare_cells": False,
            "forest_mask_has_public_osm_clip": False,
            "forest_mask_has_public_nonforest_difference": False,
            "rebuilt_nonforest_difference_available": False,
        },
    }


def _source_sql():
    return text(
        """
        WITH region_scope AS (
            SELECT
                region.code AS region_code,
                COALESCE(region.metadata->'forest_mask'->>'source_region_code', region.code) AS mask_source_region_code
            FROM regions region
            WHERE region.code = :region_code
        ),
        expected_sources AS (
            SELECT
                unnest(CAST(:layer_a_source_layers AS text[])) AS source_layer,
                'stage3_layer_a_source_check' AS pipeline,
                region_scope.mask_source_region_code AS source_region_code
            FROM region_scope
            UNION ALL
            SELECT
                unnest(CAST(:auxiliary_forest_mask_source_layers AS text[])) AS source_layer,
                'stage3_osm_forest_source_check' AS pipeline,
                region_scope.mask_source_region_code AS source_region_code
            FROM region_scope
            UNION ALL
            SELECT
                configured.source_layer,
                CASE
                    WHEN configured.source_layer = 'esa_worldcover_nonforest_exclusion'
                    THEN 'stage3_worldcover_nonforest_exclusion_source_check'
                    ELSE 'stage3_osm_nonforest_exclusion_source_check'
                END AS pipeline,
                region_scope.mask_source_region_code AS source_region_code
            FROM region_scope
            CROSS JOIN LATERAL unnest(
                CAST(:auxiliary_nonforest_exclusion_source_layers AS text[])
            ) AS configured(source_layer)
        ),
        latest AS (
            SELECT DISTINCT ON (source.source_layer)
                source.source_layer,
                source.source_type,
                source.status,
                source.feature_count,
                source.checked_at::text AS checked_at
            FROM source_checks source
            JOIN expected_sources expected
              ON expected.source_layer = source.source_layer
             AND expected.pipeline = source.metadata->>'pipeline'
             AND expected.source_region_code = source.region_code
            ORDER BY source.source_layer, source.checked_at DESC, source.id DESC
        )
        SELECT
            expected.source_layer,
            expected.source_region_code,
            COALESCE(latest.source_type, '') AS source_type,
            COALESCE(latest.status, 'missing') AS status,
            latest.feature_count,
            latest.checked_at
        FROM expected_sources expected
        LEFT JOIN latest ON latest.source_layer = expected.source_layer
        ORDER BY expected.source_layer
        """
    )


def _query_probe_sql():
    return text(
        """
        SELECT
            count(*) FILTER (WHERE result.anomaly_type = 'anomaly') AS anomaly_cells,
            count(*) FILTER (WHERE result.anomaly_type = 'watch') AS watch_cells,
            count(*) FILTER (WHERE result.anomaly_type = 'ok') AS ok_cells,
            max(result.created_at)::text AS latest_verification_at
        FROM verification_results result
        WHERE result.is_current
          AND result.metadata->>'pipeline' = 'stage1_verification'
          AND result.region_id = (
              SELECT region.id
              FROM regions region
              WHERE region.code = :region_code
          )
          AND result.grid_version = :grid_version
        """
    )


def _table_sql():
    return text(
        """
        SELECT
            expected.name,
            to_regclass('public.' || expected.name) IS NOT NULL AS present
        FROM unnest(CAST(:names AS text[])) AS expected(name)
        ORDER BY expected.name
        """
    )


def _index_sql():
    return text(
        """
        SELECT
            expected.name,
            pg_indexes.indexname IS NOT NULL AS present
        FROM unnest(CAST(:names AS text[])) AS expected(name)
        LEFT JOIN pg_indexes
          ON pg_indexes.schemaname = 'public'
         AND pg_indexes.indexname = expected.name
        ORDER BY expected.name
        """
    )


def _partition_sql():
    return text(
        """
        SELECT
            expected.name,
            partitioned.partrelid IS NOT NULL AS partitioned,
            COALESCE(pg_get_partkeydef(cls.oid), '') AS partition_key
        FROM unnest(CAST(:names AS text[])) AS expected(name)
        LEFT JOIN pg_class cls
          ON cls.oid = to_regclass('public.' || quote_ident(expected.name))
        LEFT JOIN pg_partitioned_table partitioned
          ON partitioned.partrelid = cls.oid
        ORDER BY expected.name
        """
    )


def _shadow_partition_sql():
    return text(
        """
        SELECT
            expected.name,
            partitioned.partrelid IS NOT NULL AS partitioned,
            COALESCE(pg_get_partkeydef(cls.oid), '') AS partition_key
        FROM unnest(CAST(:names AS text[])) AS expected(name)
        LEFT JOIN pg_class cls
          ON cls.oid = to_regclass('public.' || quote_ident(expected.name || '_partitioned'))
        LEFT JOIN pg_partitioned_table partitioned
          ON partitioned.partrelid = cls.oid
        ORDER BY expected.name
        """
    )


def _grid_run_sql():
    return text(
        """
        WITH component_hashes AS (
            SELECT
                component.region_id,
                component.mask_version,
                md5(
                    count(*)::text || ':' ||
                    COALESCE(sum(component.area_ha), 0)::text || ':' ||
                    COALESCE(min(component.area_ha), 0)::text || ':' ||
                    COALESCE(max(component.area_ha), 0)::text || ':' ||
                    COALESCE(ST_XMin(ST_Extent(component.geom_projected))::text, '') || ':' ||
                    COALESCE(ST_YMin(ST_Extent(component.geom_projected))::text, '') || ':' ||
                    COALESCE(ST_XMax(ST_Extent(component.geom_projected))::text, '') || ':' ||
                    COALESCE(ST_YMax(ST_Extent(component.geom_projected))::text, '')
                ) AS source_hash
            FROM stage3_region_mask_components component
            GROUP BY component.region_id, component.mask_version
        )
        SELECT
            run.grid_version,
            run.status,
            run.total_tiles,
            run.processed_tiles,
            run.inserted_cells,
            run.metadata->>'region_geometry_hash' AS run_region_geometry_hash,
            COALESCE(run.metadata->>'forest_only', 'false') = 'true' AS forest_only_run,
            COALESCE(run.metadata->>'grid_mask_method', '') AS grid_mask_method,
            COALESCE(run.metadata->'forest_mask'->>'mask_version', '') AS mask_version,
            CASE
                WHEN run.metadata->>'grid_mask_method' = 'region_mask_components'
                THEN component_hashes.source_hash
                ELSE md5(ST_AsEWKB(ST_SnapToGrid(region.geom, 0.0000001))::text)
            END AS current_region_geometry_hash,
            run.started_at::text AS started_at,
            run.finished_at::text AS finished_at
        FROM stage3_grid_generation_runs run
        JOIN regions region ON region.id = run.region_id
        LEFT JOIN component_hashes
          ON component_hashes.region_id = run.region_id
         AND component_hashes.mask_version = COALESCE(
                NULLIF(run.metadata->'forest_mask'->>'mask_version', ''),
                region.code
             )
        WHERE region.code = :region_code
          AND run.grid_version = :grid_version
        ORDER BY run.started_at DESC, run.id DESC
        LIMIT 5
        """
    )


def _layer_a_run_sql():
    return text(
        """
        WITH region_scope AS (
            SELECT COALESCE(
                NULLIF(region.metadata->'forest_mask'->>'source_region_code', ''),
                region.code
            ) AS layer_a_region_code
            FROM regions region
            WHERE region.code = :region_code
        )
        SELECT
            run.source,
            run.status,
            run.total_tiles,
            run.processed_tiles,
            run.bolme_fetched,
            run.mescere_fetched,
            run.bolme_inserted,
            run.mescere_inserted,
            run.failed_bolme,
            run.failed_mescere,
            COALESCE((run.metadata->>'max_tiles')::integer, 0) AS max_tiles,
            COALESCE((run.metadata->>'tile_shard_count')::integer, 1) AS tile_shard_count,
            COALESCE((run.metadata->>'tile_shard_index')::integer, 0) AS tile_shard_index,
            COALESCE(run.metadata->>'bbox_source', '') AS bbox_source,
            COALESCE(run.metadata->>'region_geometry_hash', '') AS run_region_geometry_hash,
            encode(digest(ST_AsEWKB(region.geom), 'sha256'), 'hex') AS current_region_geometry_hash,
            run.bbox,
            run.started_at::text AS started_at,
            run.finished_at::text AS finished_at
        FROM stage3_layer_a_ingest_runs run
        JOIN regions region ON region.id = run.region_id
        CROSS JOIN region_scope
        WHERE region.code = region_scope.layer_a_region_code
        ORDER BY run.started_at DESC, run.id DESC
        LIMIT 5
        """
    )


def _schema_status(
    table_rows: list[dict],
    index_rows: list[dict],
    partition_rows: list[dict] | None = None,
    shadow_partition_rows: list[dict] | None = None,
    shadow_partition_counts_match: dict[str, bool] | None = None,
) -> dict:
    tables = {row["name"]: bool(row["present"]) for row in table_rows}
    indexes = {row["name"]: bool(row["present"]) for row in index_rows}
    partitions = {row["name"]: bool(row["partitioned"]) for row in partition_rows or []}
    partition_keys = {row["name"]: row.get("partition_key") or "" for row in partition_rows or []}
    region_partitions = {
        name: bool(partitions.get(name)) and "region" in partition_keys.get(name, "").lower()
        for name in partitions
    }
    shadow_partitions = {row["name"]: bool(row["partitioned"]) for row in shadow_partition_rows or []}
    shadow_partition_keys = {row["name"]: row.get("partition_key") or "" for row in shadow_partition_rows or []}
    shadow_region_partitions = {
        name: bool(shadow_partitions.get(name)) and "region" in shadow_partition_keys.get(name, "").lower()
        for name in shadow_partitions
    }
    shadow_counts = shadow_partition_counts_match or {}
    active_partitioning_ready = bool(region_partitions) and all(region_partitions.values())
    shadow_partitioning_ready = bool(shadow_region_partitions) and all(
        shadow_region_partitions.get(name) and shadow_counts.get(name)
        for name in shadow_region_partitions
    )
    return {
        "tables": tables,
        "indexes": indexes,
        "partitions": partitions,
        "partition_keys": partition_keys,
        "region_partitions": region_partitions,
        "shadow_partitions": shadow_partitions,
        "shadow_partition_keys": shadow_partition_keys,
        "shadow_region_partitions": shadow_region_partitions,
        "shadow_partition_counts_match": shadow_counts,
        "active_partitioning_ready": active_partitioning_ready,
        "shadow_partitioning_ready": shadow_partitioning_ready,
        "tables_ready": all(tables.values()),
        "indexes_ready": all(indexes.values()),
        "partitioning_ready": active_partitioning_ready,
        "missing_tables": [name for name, present in tables.items() if not present],
        "missing_indexes": [name for name, present in indexes.items() if not present],
        "missing_partitions": [name for name, present in region_partitions.items() if not present],
        "missing_shadow_partitions": [
            name
            for name, present in shadow_region_partitions.items()
            if not (present and shadow_counts.get(name))
        ],
    }


def _shadow_partition_validation(db, region_code: str = "") -> dict[str, bool]:
    validation: dict[str, bool] = {}
    region_ids = _region_ids(db, region_code)
    for table_name in PARTITION_TARGETS:
        shadow_name = f"{table_name}_partitioned"
        if not _table_exists(db, table_name) or not _table_exists(db, shadow_name):
            validation[table_name] = False
            continue
        validation[table_name] = _scoped_row_count(db, table_name, region_ids) == _shadow_row_count(
            db,
            shadow_name,
            region_ids,
        )
    return validation


def _region_ids(db, region_code: str) -> list[int]:
    if not region_code:
        return []
    rows = db.execute(
        text("SELECT id FROM regions WHERE code = :region_code"),
        {"region_code": region_code},
    ).scalars().all()
    return [int(row) for row in rows]


def _table_exists(db, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _scoped_row_count(db, table_name: str, region_ids: list[int]) -> int:
    if table_name not in PARTITION_TARGETS:
        raise ValueError(f"unsupported table: {table_name}")
    if not region_ids:
        return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())
    if table_name == "grid_cells":
        return int(
            db.execute(
                text("SELECT count(*) FROM grid_cells WHERE region_id = ANY(:region_ids)"),
                {"region_ids": region_ids},
            ).scalar_one()
        )
    return int(
        db.execute(
            text(
                f"""
                SELECT count(*)
                FROM {table_name} source
                JOIN grid_cells cell ON cell.id = source.grid_cell_id
                WHERE cell.region_id = ANY(:region_ids)
                """
            ),
            {"region_ids": region_ids},
        ).scalar_one()
    )


def _shadow_row_count(db, table_name: str, region_ids: list[int]) -> int:
    if table_name not in {f"{name}_partitioned" for name in PARTITION_TARGETS}:
        raise ValueError(f"unsupported shadow table: {table_name}")
    if not region_ids:
        return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())
    return int(
        db.execute(
            text(f"SELECT count(*) FROM {table_name} WHERE region_id = ANY(:region_ids)"),
            {"region_ids": region_ids},
        ).scalar_one()
    )


def _readiness(
    summary: dict,
    sources: list[dict],
    schema: dict,
    expected_grid_cells: int = 0,
    expected_layer_a_features: int = 0,
    grid_runs: list[dict] | None = None,
    layer_a_runs: list[dict] | None = None,
    expected_source_layers: list[str] | None = None,
    performance_report_ready: bool = False,
    max_source_age_days: int = 7,
    min_ndvi_trend_coverage: float = 0.0,
    acceptance_scope: str = "full-scale",
    forest_clip: dict | None = None,
    strict_proofs: dict[str, dict] | None = None,
) -> dict:
    grid_cells = summary.get("grid_cells", 0) or 0
    carbon_assessments = summary.get("carbon_assessments", 0) or 0
    calculated_carbon = summary.get("calculated_carbon_assessments", 0) or 0
    skipped_missing_evidence_carbon = summary.get("skipped_missing_evidence_carbon_assessments", 0) or 0
    baseline_calculated_carbon = summary.get("baseline_calculated_carbon_assessments", 0) or 0
    historical_trend_carbon = summary.get("historical_trend_carbon_assessments", 0) or 0
    forest_area_carbon = summary.get("forest_area_carbon_assessments", 0) or 0
    approved_calculated_carbon = summary.get("approved_calculated_carbon_assessments", 0) or 0
    carbon_without_audit = summary.get("carbon_assessments_without_audit", 0) or 0
    verification_unexpected_sources = summary.get("verification_unexpected_layer_a_sources", 0) or 0
    verification_source_mismatches = summary.get("verification_source_layer_mismatches", 0) or 0
    layer_a_features = summary.get("layer_a_features", 0) or 0
    layer_a_ready = (summary.get("layer_a_features", 0) or 0) > 0
    grid_ready = grid_cells > 0
    verification_ready = (summary.get("verification_results", 0) or 0) > 0
    ndvi_ready = (summary.get("ndvi_observations", 0) or 0) > 0
    ndvi_trend_cells = summary.get("ndvi_trend_cells", 0) or 0
    ndvi_trend_coverage = ndvi_trend_cells / grid_cells if grid_cells else 0
    ndvi_trend_ready = grid_cells > 0 and ndvi_trend_coverage >= min_ndvi_trend_coverage
    carbon_schema_ready = "carbon_assessments" in summary
    public_scope = acceptance_scope == "public-ogm"
    carbon_calculated_complete = grid_cells > 0 and calculated_carbon >= grid_cells
    carbon_evidence_coverage_ready = (
        grid_cells > 0
        and carbon_assessments >= grid_cells
        and calculated_carbon + skipped_missing_evidence_carbon >= grid_cells
    )
    carbon_calculated_for_trend_ready = (
        grid_cells > 0
        and ndvi_trend_ready
        and calculated_carbon >= ndvi_trend_cells
    )
    if public_scope:
        carbon_calculation_ready = carbon_evidence_coverage_ready
        carbon_baseline_calculation_ready = (
            carbon_calculation_ready and baseline_calculated_carbon >= calculated_carbon
        )
        carbon_historical_trend_ready = (
            carbon_calculation_ready and historical_trend_carbon >= calculated_carbon
        )
        carbon_forest_area_ready = (
            carbon_calculation_ready and forest_area_carbon >= calculated_carbon
        )
    else:
        carbon_calculation_ready = carbon_calculated_complete
        carbon_baseline_calculation_ready = grid_cells > 0 and baseline_calculated_carbon >= grid_cells
        carbon_historical_trend_ready = grid_cells > 0 and historical_trend_carbon >= grid_cells
        carbon_forest_area_ready = grid_cells > 0 and forest_area_carbon >= grid_cells
    carbon_approved_calculation_ready = grid_cells > 0 and approved_calculated_carbon >= grid_cells
    carbon_audit_ready = carbon_without_audit == 0 if carbon_assessments else carbon_schema_ready
    expected_sources = expected_source_layers or []
    source_health = _source_health(sources, layer_a_runs or [], expected_sources, max_source_age_days)
    grid_target_ready = expected_grid_cells > 0 and grid_cells >= expected_grid_cells
    layer_a_target_ready = expected_layer_a_features > 0 and layer_a_features >= expected_layer_a_features
    schema_ready = (
        bool(schema.get("tables_ready"))
        and bool(schema.get("indexes_ready"))
        and bool(schema.get("region_metadata_column_ready", True))
    )
    active_partitioning_ready = bool(schema.get("active_partitioning_ready", schema.get("partitioning_ready")))
    shadow_partitioning_ready = bool(schema.get("shadow_partitioning_ready"))
    partitioning_ready = active_partitioning_ready or (public_scope and shadow_partitioning_ready)
    full_scale_partitioning_required = expected_grid_cells >= 1_000_000
    grid_pipeline_ready = bool(schema.get("tables", {}).get("stage3_grid_generation_runs")) and bool(
        schema.get("tables", {}).get("stage3_grid_generation_tiles")
    )
    layer_a_pipeline_ready = bool(schema.get("tables", {}).get("stage3_layer_a_ingest_runs")) and bool(
        schema.get("tables", {}).get("stage3_layer_a_ingest_tiles")
    )
    grid_run_ready = _has_completed_grid_run(grid_runs or [])
    layer_a_run_ready = _has_completed_layer_a_run(layer_a_runs or [], expected_sources)
    forest_only_region_ready = bool(summary.get("forest_only_region"))
    region_metadata = normalize_region_metadata(summary.get("region_metadata"))
    region_source = summary.get("region_source")
    filtered_component_mask_ready = _filtered_component_mask_metadata_ready(region_metadata)
    public_forest_mask_source_ready = (
        (
            region_source == FOREST_REGION_SOURCE
            and public_forest_mask_metadata_ready(region_metadata)
        )
        or (
            region_source == FILTERED_FOREST_REGION_SOURCE
            and filtered_component_mask_ready
        )
    )
    full_scale_forest_mask_source_ready = (
        region_source == FOREST_REGION_SOURCE
        and (region_metadata.get("forest_mask") or {}).get("source")
        in {
            "public_ogm_layer_a_with_osm_forest_clip",
            "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
            "official_full_layer_a",
        }
    )
    forest_mask_source_ready = (
        public_forest_mask_source_ready if public_scope else full_scale_forest_mask_source_ready
    )
    forest_only_grid_run_ready = _has_forest_only_grid_run(
        grid_runs or [],
        require_component_mask=region_source == FILTERED_FOREST_REGION_SOURCE,
    )
    forest_clip_ready = bool((forest_clip or {}).get("checks", {}).get("ready"))
    strict_proof_states = {
        name: bool((report or {}).get("checks", {}).get("ready"))
        for name, report in (strict_proofs or {}).items()
    }
    strict_stage3_proofs_ready = all(strict_proof_states.values()) if strict_proofs is not None else True
    public_source_proof_ready = bool(
        public_scope
        and strict_proofs is not None
        and strict_stage3_proofs_ready
        and layer_a_target_ready
    )
    public_layer_a_evidence_ready = layer_a_run_ready or public_source_proof_ready
    public_source_health_ready = source_health == "ok" or public_source_proof_ready
    public_pipeline_ready = (
        grid_ready
        and layer_a_ready
        and schema_ready
        and grid_pipeline_ready
        and layer_a_pipeline_ready
        and grid_run_ready
        and forest_only_region_ready
        and forest_mask_source_ready
        and forest_only_grid_run_ready
        and forest_clip_ready
        and strict_stage3_proofs_ready
        and public_layer_a_evidence_ready
        and performance_report_ready
        and public_source_health_ready
        and (partitioning_ready or not full_scale_partitioning_required)
        and grid_target_ready
        and layer_a_target_ready
    )
    full_scale_ready = (
        not public_scope
        and grid_ready
        and layer_a_ready
        and verification_ready
        and verification_unexpected_sources == 0
        and verification_source_mismatches == 0
        and ndvi_ready
        and ndvi_trend_ready
        and schema_ready
        and grid_pipeline_ready
        and layer_a_pipeline_ready
        and grid_run_ready
        and forest_only_region_ready
        and forest_mask_source_ready
        and forest_only_grid_run_ready
        and forest_clip_ready
        and strict_stage3_proofs_ready
        and layer_a_run_ready
        and performance_report_ready
        and source_health == "ok"
        and (active_partitioning_ready or not full_scale_partitioning_required)
        and grid_target_ready
        and layer_a_target_ready
        and carbon_schema_ready
        and carbon_approved_calculation_ready
        and carbon_historical_trend_ready
        and carbon_forest_area_ready
        and carbon_audit_ready
    )
    return {
        "bounded_workspace_ready": grid_ready and layer_a_ready and verification_ready and ndvi_ready,
        "verification_source_layers_ready": verification_unexpected_sources == 0
        and verification_source_mismatches == 0,
        "ndvi_trend_ready": ndvi_trend_ready,
        "ndvi_trend_coverage": round(ndvi_trend_coverage, 5),
        "min_ndvi_trend_coverage": min_ndvi_trend_coverage,
        "stage3_schema_ready": schema_ready,
        "stage3_grid_pipeline_ready": grid_pipeline_ready,
        "stage3_layer_a_pipeline_ready": layer_a_pipeline_ready,
        "stage3_grid_run_ready": grid_run_ready,
        "forest_only_region_ready": forest_only_region_ready,
        "forest_mask_source_ready": forest_mask_source_ready,
        "forest_only_grid_run_ready": forest_only_grid_run_ready,
        "forest_clip_ready": forest_clip_ready,
        "strict_stage3_proofs_ready": strict_stage3_proofs_ready,
        "strict_stage3_proof_reports": strict_proof_states,
        "stage3_layer_a_run_ready": layer_a_run_ready,
        "public_source_proof_ready": public_source_proof_ready,
        "public_layer_a_evidence_ready": public_layer_a_evidence_ready,
        "public_source_health_ready": public_source_health_ready,
        "performance_report_ready": performance_report_ready,
        "carbon_foundation_ready": carbon_schema_ready,
        "carbon_calculation_ready": carbon_calculation_ready,
        "carbon_baseline_calculation_ready": carbon_baseline_calculation_ready,
        "carbon_historical_trend_ready": carbon_historical_trend_ready,
        "carbon_forest_area_ready": carbon_forest_area_ready,
        "carbon_approved_calculation_ready": carbon_approved_calculation_ready,
        "carbon_audit_ready": carbon_audit_ready,
        "carbon_assessment_coverage": round(carbon_assessments / grid_cells, 5) if grid_cells else 0,
        "calculated_carbon_coverage": round(calculated_carbon / grid_cells, 5) if grid_cells else 0,
        "carbon_evidence_coverage": round(
            min((calculated_carbon + skipped_missing_evidence_carbon) / grid_cells, 1), 5
        )
        if grid_cells
        else 0,
        "skipped_missing_evidence_carbon_assessments": skipped_missing_evidence_carbon,
        "calculated_carbon_complete": carbon_calculated_complete,
        "source_health": source_health,
        "target_grid_ready": grid_target_ready,
        "target_layer_a_ready": layer_a_target_ready,
        "partitioning_ready": partitioning_ready,
        "active_partitioning_ready": active_partitioning_ready,
        "shadow_partitioning_ready": shadow_partitioning_ready,
        "full_scale_partitioning_required": full_scale_partitioning_required,
        "public_ogm_pipeline_ready": public_pipeline_ready,
        "official_full_layer_a_backlog": public_scope,
        "full_scale_ready": full_scale_ready,
    }


def _contract_checklist(report: dict) -> dict:
    readiness = report["readiness"]
    summary = report["summary"]
    schema = report["schema"]
    scope = report["scope"]
    operational = report.get("operational") or {}
    acceptance_scope = scope.get("acceptance_scope") or "full-scale"
    public_scope = acceptance_scope == "public-ogm"
    expected_grid_cells = scope.get("expected_grid_cells") or 0
    expected_layer_a_features = scope.get("expected_layer_a_features") or 0

    items = {
        "ogm_layer_a_pipeline": (
            readiness["source_health"] == "ok" and summary["layer_a_features"] > 0
        )
        or bool(public_scope and readiness.get("public_source_proof_ready")),
        "grid_pipeline": readiness.get("stage3_grid_pipeline_ready", False),
        "layer_a_run_tracking": readiness.get("stage3_layer_a_pipeline_ready", False),
        "completed_grid_run": readiness.get("stage3_grid_run_ready", False),
        "forest_only_region": readiness.get("forest_only_region_ready", False),
        "forest_mask_source": readiness.get("forest_mask_source_ready", False),
        "forest_only_grid_run": readiness.get("forest_only_grid_run_ready", False),
        "forest_clip": readiness.get("forest_clip_ready", False),
        "strict_stage3_proofs": readiness.get("strict_stage3_proofs_ready", True),
        "completed_layer_a_run": (
            readiness.get("public_layer_a_evidence_ready", False)
            if public_scope
            else readiness.get("stage3_layer_a_run_ready", False)
        ),
        "performance_report": readiness.get("performance_report_ready", False),
        "postgis_schema_and_indexes": readiness["stage3_schema_ready"],
        "carbon_foundation": readiness["carbon_foundation_ready"],
        "carbon_calculation": readiness["carbon_calculation_ready"],
        "carbon_baseline_calculation": readiness.get("carbon_baseline_calculation_ready", False),
        "carbon_historical_trend": readiness.get("carbon_historical_trend_ready", False),
        "carbon_forest_area_basis": readiness.get("carbon_forest_area_ready", False),
        "carbon_audit": readiness["carbon_audit_ready"],
        "ndvi_trend_history": readiness.get("ndvi_trend_ready", True),
        "ogm_tls_verify": bool(operational.get("ogm_tls_verify", True)),
        "future_layer_a_adapter_boundary": _future_layer_a_adapter_boundary_ready(),
        "official_full_layer_a_backlog": public_scope,
    }
    if public_scope:
        items["public_full_scale_rollout"] = expected_grid_cells >= PUBLIC_CONTRACT_ACCEPTANCE_MIN_CELLS
    if not public_scope:
        items["full_layer_a_target_loaded"] = readiness["target_layer_a_ready"]
        items["full_grid_target_loaded"] = readiness["target_grid_ready"]
        items["postgis_partitioning"] = readiness["partitioning_ready"]
        items["carbon_approved_calculation"] = readiness.get("carbon_approved_calculation_ready", False)
    blockers = []
    if not expected_grid_cells:
        blockers.append("expected grid cell target was not supplied")
    elif not readiness["target_grid_ready"]:
        blockers.append(f"grid target not loaded: {summary['grid_cells']} of {expected_grid_cells}")
    if public_scope and 0 < expected_grid_cells < PUBLIC_CONTRACT_ACCEPTANCE_MIN_CELLS:
        blockers.append(
            "public OGM/OSM scope is pilot-sized; full public forest-only rollout evidence is still required"
        )
    if not expected_layer_a_features:
        blockers.append("expected Layer A feature target was not supplied")
    elif not readiness["target_layer_a_ready"]:
        blockers.append(f"Layer A target not loaded: {summary['layer_a_features']} of {expected_layer_a_features}")
    if not schema["tables_ready"]:
        blockers.append(f"missing tables: {', '.join(schema['missing_tables'])}")
    if not schema["indexes_ready"]:
        blockers.append(f"missing indexes: {', '.join(schema['missing_indexes'])}")
    if not schema.get("region_metadata_column_ready", True):
        blockers.append("regions metadata migration is missing")
    if readiness.get("full_scale_partitioning_required", False) and not readiness["partitioning_ready"]:
        blockers.append(f"missing partitioning: {', '.join(schema['missing_partitions'])}")
    if readiness["source_health"] != "ok" and not (
        public_scope and readiness.get("public_source_proof_ready", False)
    ):
        blockers.append("latest Layer A source health is not ok")
    if not public_scope and not readiness.get("bounded_workspace_ready", True):
        if not (summary.get("verification_results", 0) or 0):
            blockers.append("verification results are missing for the selected scope")
        if not (summary.get("ndvi_observations", 0) or 0):
            blockers.append("usable NDVI observations are missing for the selected scope")
    if not readiness.get("verification_source_layers_ready", True):
        blockers.append("verification results do not match the accepted public OGM forest-mask source set")
    if not public_scope and "ndvi_trend_ready" in readiness and not readiness.get("ndvi_trend_ready", False):
        blockers.append(
            "NDVI trend history does not meet the selected coverage threshold: "
            f"{summary.get('ndvi_trend_cells', 0) or 0} of {summary.get('grid_cells', 0) or 0}"
        )
    if not readiness.get("stage3_layer_a_pipeline_ready", False):
        blockers.append("Layer A ingest run tracking is not ready")
    if not readiness.get("stage3_grid_pipeline_ready", False):
        blockers.append("grid generation run tracking is not ready")
    if not readiness.get("stage3_layer_a_run_ready", False) and not (
        public_scope and readiness.get("public_layer_a_evidence_ready", False)
    ):
        blockers.append("no successful complete Layer A ingest run")
    if not readiness.get("stage3_grid_run_ready", False):
        blockers.append("no successful complete grid generation run")
    if not readiness.get("forest_only_region_ready", False):
        blockers.append("selected Stage 3 region is not marked as forest-only")
    if not readiness.get("forest_mask_source_ready", False):
        blockers.append("selected Stage 3 region is not sourced from an accepted public forest mask")
    if not readiness.get("forest_only_grid_run_ready", False):
        blockers.append("completed grid generation run is not marked as forest-only")
    if not readiness.get("forest_clip_ready", False):
        blockers.append("forest clip check did not prove full 1 ha cells inside the public forest mask")
    if not readiness.get("strict_stage3_proofs_ready", True):
        failed_reports = [
            name
            for name, ready in (readiness.get("strict_stage3_proof_reports") or {}).items()
            if not ready
        ]
        blockers.append(f"strict Stage 3 proof reports are missing or failed: {', '.join(failed_reports) or 'unknown'}")
    if not readiness.get("performance_report_ready", False):
        blockers.append("Stage 3 performance report is missing or failed")
    if not public_scope and not readiness["carbon_calculation_ready"]:
        if public_scope:
            blockers.append(
                "carbon evidence does not cover every current grid cell as calculated or explicitly skipped"
            )
        else:
            blockers.append("carbon assessments are not calculated for every current grid cell")
    if not public_scope and not readiness.get("carbon_baseline_calculation_ready", False):
        if public_scope:
            blockers.append("calculated carbon assessments do not all use baseline or approved coefficients")
        else:
            blockers.append(
                "carbon assessments are not calculated from baseline or approved coefficients for every current grid cell"
            )
    if not public_scope and not readiness.get("carbon_historical_trend_ready", False):
        if public_scope:
            blockers.append("calculated carbon assessments do not all use historical NDVI trend evidence")
        else:
            blockers.append("carbon assessments do not use historical NDVI trend evidence for every current grid cell")
    if not public_scope and not readiness.get("carbon_forest_area_ready", False):
        if public_scope:
            blockers.append("calculated carbon assessments do not all use Layer A forest-area basis")
        else:
            blockers.append("carbon assessments do not use Layer A forest-area basis for every current grid cell")
    if not public_scope and not readiness.get("carbon_approved_calculation_ready", False):
        blockers.append("approved carbon calculations are missing for full-scale acceptance")
    if not public_scope and not readiness["carbon_audit_ready"]:
        blockers.append("carbon audit events are missing for current carbon assessments")
    if not operational.get("ogm_tls_verify", True):
        blockers.append("OGM TLS verification is disabled")

    return {
        "items": items,
        "blockers": blockers,
        "ready_for_contract_acceptance": not blockers,
    }


def _stage3_acceptance_summary(report: dict) -> dict:
    checklist = report["contract_checklist"]
    readiness = report["readiness"]
    summary = report["summary"]
    scope = report["scope"]
    acceptance_scope = scope.get("acceptance_scope") or "full-scale"
    public_scope = acceptance_scope == "public-ogm"
    completed_items = [name for name, ready in checklist["items"].items() if ready]
    pending_items = [name for name, ready in checklist["items"].items() if not ready]
    target_grid_cells = scope.get("expected_grid_cells") or 0
    target_layer_a = scope.get("expected_layer_a_features") or 0
    grid_progress = _progress(summary.get("grid_cells", 0) or 0, target_grid_cells)
    layer_a_progress = _progress(summary.get("layer_a_features", 0) or 0, target_layer_a)
    trend_progress = _progress(
        summary.get("ndvi_trend_cells", 0) or 0,
        summary.get("grid_cells", 0) or 0,
    )
    if public_scope:
        stage = "public_ogm_pipeline_ready" if checklist["ready_for_contract_acceptance"] else "public_pipeline_incomplete"
    else:
        stage = "full_scale_ready" if checklist["ready_for_contract_acceptance"] else "ready_for_full_scale_run"
    if not readiness["stage3_grid_pipeline_ready"] or not readiness["stage3_layer_a_pipeline_ready"]:
        stage = "foundation_incomplete"
    elif not readiness.get("stage3_grid_run_ready", False) or not readiness.get("stage3_layer_a_run_ready", False):
        stage = "pipeline_ready_not_loaded"
    return {
        "status": stage,
        "acceptance_scope": acceptance_scope,
        "completed_items": completed_items,
        "pending_items": pending_items,
        "grid_progress": grid_progress,
        "layer_a_progress": layer_a_progress,
        "ndvi_trend_progress": trend_progress,
        "can_continue_without_schema_changes": (
            readiness["stage3_schema_ready"]
            and readiness["stage3_grid_pipeline_ready"]
            and readiness["stage3_layer_a_pipeline_ready"]
        ),
        "requires_full_scale_run": (
            public_scope
            and not checklist["items"].get("public_full_scale_rollout", True)
        )
        or ((not public_scope) and not checklist["ready_for_contract_acceptance"]),
        "official_full_layer_a_status": "backlog" if public_scope else "required",
        "forest_only_scope": bool(readiness.get("forest_only_region_ready"))
        and bool(readiness.get("forest_only_grid_run_ready")),
    }


def _progress(current: int, target: int) -> dict:
    if target <= 0:
        return {
            "current": current,
            "target": None,
            "ratio": None,
        }
    return {
        "current": current,
        "target": target,
        "ratio": round(min(current / target, 1), 6),
    }


def _future_layer_a_adapter_boundary_ready() -> bool:
    strategy = stage1_source_strategy()
    extension = strategy.get("extension_model") or {}
    future_sources = extension.get("future_adapter_sources") or []
    return (
        extension.get("integration_boundary") == "normalized Layer A records"
        and extension.get("closed_layers_status") == "backlog"
        and any(
            source.get("status") == "backlog"
            and source.get("source_quality") == "official_full_layer_a"
            and bool(source.get("expected_input"))
            for source in future_sources
        )
    )


def _has_completed_grid_run(runs: list[dict]) -> bool:
    for run in runs:
        if _grid_run_matches_current_region(run):
            return True
    return False


def _has_forest_only_grid_run(runs: list[dict], *, require_component_mask: bool = False) -> bool:
    for run in runs:
        if not _grid_run_matches_current_region(run) or not bool(run.get("forest_only_run")):
            continue
        if require_component_mask:
            if run.get("grid_mask_method") != "region_mask_components":
                continue
            if not run.get("mask_version"):
                continue
        return True
    return False


def _filtered_component_mask_metadata_ready(metadata: dict) -> bool:
    if not public_forest_mask_metadata_ready(metadata):
        return False
    forest_mask = metadata.get("forest_mask") or {}
    policy = forest_mask.get("clean_mask_policy") or {}
    if forest_mask.get("mask_components_table") != "stage3_region_mask_components":
        return False
    if not forest_mask.get("mask_version"):
        return False
    if policy.get("applies_before_grid_insert") is not True:
        return False
    try:
        min_area_ha = float(policy.get("min_component_area_ha") or 0)
    except (TypeError, ValueError):
        return False
    return min_area_ha > 0


def _grid_run_matches_current_region(run: dict) -> bool:
    if run.get("status") != "completed":
        return False
    if int(run.get("inserted_cells") or 0) <= 0:
        return False
    if int(run.get("total_tiles") or 0) <= 0:
        return False
    if int(run.get("processed_tiles") or 0) < int(run.get("total_tiles") or 0):
        return False
    run_hash = run.get("run_region_geometry_hash")
    current_hash = run.get("current_region_geometry_hash")
    if run_hash and current_hash and run_hash != current_hash:
        return False
    return True


def _has_completed_layer_a_run(runs: list[dict], expected_source_layers: list[str] | None = None) -> bool:
    required_run_sources = _required_layer_a_run_sources(expected_source_layers)
    completed_run_sources: set[str] = set()
    shard_groups: dict[tuple, set[int]] = {}
    shard_targets: dict[tuple, int] = {}
    for run in runs:
        if not _is_completed_layer_a_run_fragment(run):
            continue
        if not _layer_a_run_source_has_features(run):
            continue
        shard_count = int(run.get("tile_shard_count") or 1)
        shard_index = int(run.get("tile_shard_index") or 0)
        if shard_count == 1:
            completed_run_sources.add(run.get("source"))
            continue
        if shard_index < 0 or shard_index >= shard_count:
            continue
        key = (
            run.get("source"),
            shard_count,
            run.get("bbox_source") or "region",
            run.get("run_region_geometry_hash") or "",
            run.get("current_region_geometry_hash") or "",
            str(run.get("bbox") or ""),
        )
        shard_groups.setdefault(key, set()).add(shard_index)
        shard_targets[key] = shard_count
    for key in shard_groups:
        if shard_groups[key] == set(range(shard_targets[key])):
            completed_run_sources.add(key[0])
    return _layer_a_run_sources_cover(completed_run_sources, required_run_sources)


def _required_layer_a_run_sources(expected_source_layers: list[str] | None = None) -> set[str]:
    if expected_source_layers is None:
        return {"bolme", "mescere"}
    required_sources = {
        run_source
        for source_layer, run_source in LAYER_A_RUN_SOURCE_BY_SOURCE_LAYER.items()
        if source_layer in expected_source_layers
    }
    return required_sources or {"bolme", "mescere"}


def _layer_a_run_sources_cover(completed_sources: set[str], required_sources: set[str]) -> bool:
    if not completed_sources:
        return False
    if "all" in completed_sources:
        return required_sources.issubset({"bolme", "mescere"})
    return required_sources.issubset(completed_sources)


def _is_completed_layer_a_run_fragment(run: dict) -> bool:
    if run.get("status") != "completed":
        return False
    if int(run.get("total_tiles") or 0) <= 0:
        return False
    if int(run.get("processed_tiles") or 0) < int(run.get("total_tiles") or 0):
        return False
    if int(run.get("failed_bolme") or 0) or int(run.get("failed_mescere") or 0):
        return False
    if int(run.get("max_tiles") or 0):
        return False
    if run.get("bbox_source") and run.get("bbox_source") != "region":
        return False
    return True


def _layer_a_run_source_has_features(run: dict) -> bool:
    source = run.get("source")
    if source == "all":
        return int(run.get("bolme_inserted") or 0) > 0 and int(run.get("mescere_inserted") or 0) > 0
    if source == "bolme":
        return int(run.get("bolme_inserted") or 0) > 0
    if source == "mescere":
        return int(run.get("mescere_inserted") or 0) > 0
    return False


def _source_health(
    sources: list[dict],
    layer_a_runs: list[dict],
    expected_sources: list[str],
    max_age_days: int = 7,
) -> str:
    source_statuses = {source.get("source_layer"): source.get("status") for source in sources}
    if expected_sources and all(source_statuses.get(source_layer) == "ok" for source_layer in expected_sources):
        stale_sources = [
            source.get("source_layer")
            for source in sources
            if source.get("source_layer") in expected_sources
            and _source_age_days(source.get("checked_at")) is not None
            and _source_age_days(source.get("checked_at")) > max_age_days
        ]
        if stale_sources:
            return "stale"
        return "ok"
    return "retained_or_pending"


def _source_age_days(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        checked_at = value
    else:
        try:
            checked_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - checked_at.astimezone(UTC)).total_seconds() // 86400))


def _tracking_tables_ready(schema: dict, *names: str) -> bool:
    tables = schema.get("tables", {})
    return all(bool(tables.get(name)) for name in names)


def _load_performance_report(
    path: str,
    expected_scope: dict | None = None,
    expected_table_counts: dict | None = None,
    required_probe_names: set[str] | None = None,
) -> dict:
    if not path:
        return {"present": False, "ready": False}
    report_path = Path(path)
    if not report_path.exists():
        return {"present": False, "ready": False, "path": str(report_path)}
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "ready": False,
            "path": str(report_path),
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
        }
    scope = payload.get("scope") or {}
    scope_matches = True
    if expected_scope:
        scope_matches = all(scope.get(key) == value for key, value in expected_scope.items())
    probes = payload.get("probes") or []
    probe_names = {probe.get("name") for probe in probes if isinstance(probe, dict)}
    thresholds_present = bool(payload.get("max_avg_ms")) and bool(payload.get("max_p95_ms"))
    max_avg_ms = float(payload.get("max_avg_ms") or 0)
    max_p95_ms = float(payload.get("max_p95_ms") or 0)
    table_counts_match = True
    if expected_table_counts:
        report_counts = payload.get("table_counts") or {}
        table_counts_match = all(
            int(report_counts.get(key) or 0) == int(value or 0)
            for key, value in expected_table_counts.items()
        )
    required_probes = required_probe_names or REQUIRED_PERFORMANCE_PROBES
    required_probes_present = required_probes.issubset(probe_names)
    non_empty_probes = all(
        int(probe.get("result_count") or 0) > 0
        for probe in probes
        if isinstance(probe, dict) and probe.get("name") in required_probes
    )
    explain_present = all(
        "plan" in probe
        for probe in probes
        if isinstance(probe, dict) and probe.get("name") in required_probes
    )
    thresholds_pass = all(
        float(probe.get("avg_ms") or 0) <= max_avg_ms and float(probe.get("p95_ms") or 0) <= max_p95_ms
        for probe in probes
        if isinstance(probe, dict) and probe.get("name") in required_probes
    )
    return {
        "present": True,
        "ready": (
            payload.get("status") == "ok"
            and bool(probes)
            and scope_matches
            and table_counts_match
            and thresholds_present
            and thresholds_pass
            and required_probes_present
            and non_empty_probes
            and explain_present
        ),
        "path": str(report_path),
        "status": payload.get("status"),
        "scope_matches": scope_matches,
        "table_counts_match": table_counts_match,
        "thresholds_present": thresholds_present,
        "thresholds_pass": thresholds_pass,
        "required_probes_present": required_probes_present,
        "non_empty_probes": non_empty_probes,
        "explain_present": explain_present,
        "probe_count": len(probes),
        "probe_names": sorted(probe_names),
        "required_probe_names": sorted(required_probes),
        "missing_probes": sorted(required_probes - probe_names),
    }


def _required_performance_probes(acceptance_scope: str) -> set[str]:
    if acceptance_scope == "public-ogm":
        return PUBLIC_REQUIRED_PERFORMANCE_PROBES
    return REQUIRED_PERFORMANCE_PROBES


def _load_forest_clip_report(path: str, expected_scope: dict | None = None) -> dict | None:
    if not path:
        return None
    report_path = Path(path)
    if not report_path.exists():
        return {
            "present": False,
            "checks": {"ready": False},
            "path": str(report_path),
            "error": "forest clip report is missing",
        }
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "checks": {"ready": False},
            "path": str(report_path),
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
        }
    scope_matches = True
    if expected_scope:
        scope_matches = all(payload.get(key) == value for key, value in expected_scope.items())
    payload["present"] = True
    payload["path"] = str(report_path)
    payload["scope_matches"] = scope_matches
    if not scope_matches:
        checks = dict(payload.get("checks") or {})
        checks["ready"] = False
        payload["checks"] = checks
    return payload


def _load_ready_report(path: str, expected_scope: dict | None = None, label: str = "report") -> dict:
    if not path:
        return {
            "present": False,
            "checks": {"ready": False},
            "error": f"{label} report is missing",
        }
    report_path = Path(path)
    if not report_path.exists():
        return {
            "present": False,
            "checks": {"ready": False},
            "path": str(report_path),
            "error": f"{label} report is missing",
        }
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "checks": {"ready": False},
            "path": str(report_path),
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
        }
    scope_matches = True
    if expected_scope:
        scope_matches = all(payload.get(key) == value for key, value in expected_scope.items())
    checks = dict(payload.get("checks") or {})
    checks["ready"] = bool(checks.get("ready")) and scope_matches
    payload["checks"] = checks
    payload["present"] = True
    payload["path"] = str(report_path)
    payload["scope_matches"] = scope_matches
    return payload


if __name__ == "__main__":
    main()
