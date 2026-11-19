from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.api.stage1 import stage1_params
from app.commands.check_stage3_forest_clip import forest_clip_report
from app.commands.run_stage3_readiness import _load_forest_clip_report, _load_performance_report, _load_ready_report
from app.db.session import SessionLocal
from app.services.source_strategy import stage1_source_strategy
from app.services.stage3_public_v9 import stage3_public_v9_manifest_hash


CONTRACT_TARGET_HECTARES = 23_000_000
CONTRACT_GRID_SIZE_M = 100

CONTRACT_ITEMS = (
    "isolated_ogm_parser",
    "layer_a_full_load",
    "forest_only_grid_generation",
    "full_grid_load",
    "gist_indexes",
    "region_partitioning",
    "query_tuning",
    "frontend_performance_tests",
    "admin_performance_tests",
)

REQUIRED_GIST_INDEXES = (
    "regions_geom_gix",
    "grid_cells_geom_gix",
    "ogm_parcels_geom_gix",
)

REQUIRED_QUERY_INDEXES = (
    "grid_cells_region_version_uuid_idx",
    "ogm_parcels_current_region_layer_idx",
    "sentinel_observations_stage_region_grid_cell_date_idx",
    "sentinel_observations_scope_grid_cell_date_idx",
    "verification_results_stage_region_grid_cell_idx",
    "verification_results_scope_grid_cell_idx",
    "verification_results_scope_status_idx",
    "verification_audit_events_stage_region_grid_cell_idx",
    "verification_audit_events_scope_cell_created_idx",
)

REQUIRED_PARTITION_TARGETS = (
    "grid_cells",
    "sentinel_observations",
    "verification_results",
)

FRONTEND_PROBES = (
    "asset_register_page",
    "asset_register_count",
    "raw_grid_mvt_tile",
    "layer_b_mvt_tile",
    "layer_c_mvt_tile",
)

ADMIN_PROBES = (
    "layer_a_intersections",
    "latest_ndvi_history",
    "carbon_asset_page",
    "carbon_audit_tail",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Stage 3 against the contract checklist")
    parser.add_argument("--region-code", default="")
    parser.add_argument("--grid-version", default="")
    parser.add_argument("--acceptance-scope", choices=("public-ogm", "full-scale"), default="public-ogm")
    parser.add_argument("--target-hectares", type=float, default=CONTRACT_TARGET_HECTARES)
    parser.add_argument("--grid-size-m", type=int, default=CONTRACT_GRID_SIZE_M)
    parser.add_argument("--expected-grid-cells", type=int, default=0)
    parser.add_argument("--expected-layer-a-features", type=int, default=0)
    parser.add_argument("--expected-ogm-tiles", type=int, default=0)
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
    if args.target_hectares <= 0:
        raise ValueError("target-hectares must be greater than zero")
    if args.grid_size_m <= 0:
        raise ValueError("grid-size-m must be greater than zero")
    if args.expected_grid_cells < 0:
        raise ValueError("expected-grid-cells must not be negative")
    if args.expected_layer_a_features < 0:
        raise ValueError("expected-layer-a-features must not be negative")
    if args.expected_ogm_tiles < 0:
        raise ValueError("expected-ogm-tiles must not be negative")

    params = stage1_params()
    if args.region_code:
        params["region_code"] = args.region_code
    if args.grid_version:
        params["grid_version"] = args.grid_version
    params["layer_a_source_layers"] = params.get("forest_mask_source_layers") or params["layer_a_source_layers"]

    with SessionLocal() as db:
        state = _load_state(db, params)
        schema = _load_schema(db, params["region_code"])
        forest_clip = _load_forest_clip_report(
            args.forest_clip_report,
            {"region_code": params["region_code"], "grid_version": params["grid_version"]},
        ) or forest_clip_report(
            db,
            region_code=params["region_code"],
            grid_version=params["grid_version"],
        )

    contract_target_grid_cells = estimate_contract_grid_cells(args.target_hectares, args.grid_size_m)
    acceptance_grid_target = _acceptance_grid_target(
        acceptance_scope=args.acceptance_scope,
        expected_grid_cells=args.expected_grid_cells,
        contract_target_grid_cells=contract_target_grid_cells,
    )
    performance = _load_performance_report(
        args.performance_report,
        {"region_code": params["region_code"], "grid_version": params["grid_version"]},
        _performance_expected_table_counts(state, args.acceptance_scope),
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
    checklist = build_contract_checklist(
        state=state,
        schema=schema,
        performance_report=performance,
        target_grid_cells=acceptance_grid_target,
        contract_target_grid_cells=contract_target_grid_cells,
        expected_layer_a_features=args.expected_layer_a_features,
        expected_ogm_tiles=args.expected_ogm_tiles,
        acceptance_scope=args.acceptance_scope,
        forest_clip_report=forest_clip,
        strict_proof_reports=strict_proof_reports,
    )
    report = {
        "stage": "stage3",
        "scope": {
            "region_code": params["region_code"],
            "grid_version": params["grid_version"],
            "acceptance_scope": args.acceptance_scope,
            "target_hectares": args.target_hectares,
            "contract_target_grid_cells": contract_target_grid_cells,
            "acceptance_grid_target": acceptance_grid_target,
            "expected_grid_cells": args.expected_grid_cells or None,
            "expected_layer_a_features": args.expected_layer_a_features or None,
            "expected_ogm_tiles": args.expected_ogm_tiles or None,
        },
        "state": state,
        "schema": schema,
        "forest_clip_report": forest_clip,
        "strict_proof_reports": strict_proof_reports,
        "performance_report": performance,
        "contract_checklist": checklist,
    }
    payload = json.dumps(report, indent=2, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if args.fail_on_not_ready and not checklist["ready"]:
        raise SystemExit("Stage 3 contract audit did not pass")


def estimate_contract_grid_cells(target_hectares: float, grid_size_m: int) -> int:
    cell_area_ha = (grid_size_m * grid_size_m) / 10_000
    return math.ceil(target_hectares / cell_area_ha)


def _acceptance_grid_target(
    *,
    acceptance_scope: str,
    expected_grid_cells: int,
    contract_target_grid_cells: int,
) -> int:
    if acceptance_scope == "public-ogm" and expected_grid_cells > 0:
        return expected_grid_cells
    return contract_target_grid_cells


def build_contract_checklist(
    *,
    state: dict[str, Any],
    schema: dict[str, Any],
    performance_report: dict[str, Any],
    target_grid_cells: int,
    expected_layer_a_features: int,
    contract_target_grid_cells: int | None = None,
    expected_ogm_tiles: int = 0,
    acceptance_scope: str = "public-ogm",
    forest_clip_report: dict[str, Any] | None = None,
    strict_proof_reports: dict[str, dict] | None = None,
) -> dict[str, Any]:
    probe_names = set(performance_report.get("probe_names") or [])
    if not probe_names and performance_report.get("probes"):
        probe_names = {probe.get("name", "") for probe in performance_report["probes"]}
    contract_grid_target = (
        target_grid_cells
        if acceptance_scope == "public-ogm"
        else contract_target_grid_cells or target_grid_cells
    )
    partitioning_required = contract_grid_target >= 1_000_000 or acceptance_scope == "full-scale"
    forest_clip_ready = bool((forest_clip_report or {}).get("checks", {}).get("ready"))
    strict_proof_states = {
        name: bool((report or {}).get("checks", {}).get("ready"))
        for name, report in (strict_proof_reports or {}).items()
    }
    strict_proofs_ready = all(strict_proof_states.values()) if strict_proof_reports is not None else True
    public_current_source_proofs_ready = bool(strict_proof_reports is not None and strict_proofs_ready)
    public_small_scope = acceptance_scope == "public-ogm" and target_grid_cells < 1_000_000
    required_frontend_probes = (
        ("asset_register_page", "asset_register_count", "raw_grid_mvt_tile")
        if acceptance_scope == "public-ogm"
        else FRONTEND_PROBES
    )
    required_admin_probes = (
        ("layer_a_intersections",)
        if acceptance_scope == "public-ogm"
        else ADMIN_PROBES
    )
    items = {
        "isolated_ogm_parser": bool(schema["layer_a_ingest_tracking"] and schema["source_adapter_boundary"]),
        "layer_a_full_load": _layer_a_load_ready(
            state,
            expected_layer_a_features,
            expected_ogm_tiles,
            acceptance_scope,
            public_current_source_proofs_ready=public_current_source_proofs_ready,
        ),
        "forest_only_grid_generation": bool(
            state["forest_only_region"]
            and state["forest_only_grid_run"]
            and forest_clip_ready
            and strict_proofs_ready
        ),
        "strict_stage3_proofs": strict_proofs_ready,
        "full_grid_load": state["grid_cells"] >= contract_grid_target,
        "gist_indexes": all(schema["indexes"].get(name, False) for name in REQUIRED_GIST_INDEXES),
        "region_partitioning": all(
            _partition_ready(schema, name, acceptance_scope=acceptance_scope)
            for name in REQUIRED_PARTITION_TARGETS
        ),
        "query_tuning": all(schema["indexes"].get(name, False) for name in REQUIRED_QUERY_INDEXES),
        "frontend_performance_tests": performance_report.get("ready", False)
        and set(required_frontend_probes).issubset(probe_names),
        "admin_performance_tests": performance_report.get("ready", False)
        and set(required_admin_probes).issubset(probe_names),
    }
    blockers = _contract_blockers(
        items,
        state,
        schema,
        target_grid_cells,
        contract_grid_target,
        expected_layer_a_features,
        expected_ogm_tiles,
        performance_report,
        acceptance_scope,
        partitioning_required,
        forest_clip_report,
        strict_proof_reports,
        public_small_scope,
    )
    progress = {
        "grid_cells": _progress(state["grid_cells"], target_grid_cells),
        "contract_grid_reference": _progress(state["grid_cells"], contract_grid_target),
        "layer_a_features": _progress(
            _public_layer_a_feature_count(state, acceptance_scope),
            expected_layer_a_features,
        ),
        "public_ogm_tiles": _progress(state["public_layer_a_processed_tiles"], state["public_layer_a_total_tiles"]),
        "public_ogm_tile_plan": _progress(state["public_layer_a_total_tiles"], expected_ogm_tiles),
        "completed_items": [name for name in CONTRACT_ITEMS if items[name]],
        "pending_items": [name for name in CONTRACT_ITEMS if not items[name]],
        "acceptance_mode": "public-pilot" if public_small_scope else acceptance_scope,
    }
    return {
        "items": items,
        "progress": progress,
        "blockers": blockers,
        "ready": not blockers,
    }


def _contract_blockers(
    items: dict[str, bool],
    state: dict[str, Any],
    schema: dict[str, Any],
    target_grid_cells: int,
    contract_target_grid_cells: int,
    expected_layer_a_features: int,
    expected_ogm_tiles: int,
    performance_report: dict[str, Any],
    acceptance_scope: str,
    partitioning_required: bool,
    forest_clip_report: dict[str, Any] | None,
    strict_proof_reports: dict[str, dict] | None,
    public_small_scope: bool,
) -> list[str]:
    blockers: list[str] = []
    if not items["isolated_ogm_parser"]:
        blockers.append("isolated OGM parser or source adapter boundary is not ready")
    if not items["layer_a_full_load"]:
        if acceptance_scope == "public-ogm":
            if not _public_source_checks_ready(state):
                blockers.append("public OGM/OSM source checks are incomplete")
            if (
                expected_ogm_tiles
                and int(state["public_layer_a_total_tiles"] or 0) < expected_ogm_tiles
            ):
                blockers.append(
                    "public OGM Layer A tile coverage is incomplete: "
                    f"{int(state['public_layer_a_total_tiles'] or 0)} of {expected_ogm_tiles}"
                )
            if int(state["layer_a_features"] or 0) <= 0:
                blockers.append("public OGM/OSM Layer A load has no current features")
            if state["osm_forest_features"] <= 0:
                blockers.append("public OSM forest mask has no current features")
            if not _nonforest_difference_ready(state):
                blockers.append("public OSM non-forest Difference mask has no completed polygon and line run")
        else:
            if not _public_layer_a_source_ready(state):
                blockers.append("public OGM/OSM Layer A source has no completed full-scale run")
            if (
                expected_ogm_tiles
                and int(state["public_layer_a_total_tiles"] or 0) < expected_ogm_tiles
            ):
                blockers.append(
                    "public OGM Layer A full-scale tile coverage is incomplete: "
                    f"{int(state['public_layer_a_total_tiles'] or 0)} of {expected_ogm_tiles}"
                )
            if expected_layer_a_features and state["layer_a_features"] < expected_layer_a_features:
                blockers.append(f"Layer A full load is incomplete: {state['layer_a_features']} of {expected_layer_a_features}")
            if int(state["layer_a_features"] or 0) <= 0:
                blockers.append("public OGM/OSM Layer A full-scale load has no current features")
            if state["osm_forest_features"] <= 0:
                blockers.append("public OSM forest mask has no current features")
            if not _nonforest_difference_ready(state):
                blockers.append("public OSM non-forest Difference mask has no completed full-scale polygon and line run")
    if not items["forest_only_grid_generation"]:
        blockers.append("forest-only grid generation has not been proven")
    if forest_clip_report is None:
        blockers.append("forest clip report is missing")
    elif not (forest_clip_report.get("checks") or {}).get("ready"):
        failed_checks = [
            name
            for name, ready in (forest_clip_report.get("checks") or {}).items()
            if name != "ready" and not ready
        ]
        blockers.append(f"forest clip check failed: {', '.join(failed_checks) or 'unknown'}")
    if strict_proof_reports is not None:
        failed_reports = [
            name
            for name, report in strict_proof_reports.items()
            if not (report.get("checks") or {}).get("ready")
        ]
        if failed_reports:
            blockers.append(f"strict Stage 3 proof reports are missing or failed: {', '.join(failed_reports)}")
    if public_small_scope:
        blockers.append(
            "public OGM/OSM scope is pilot-sized; full public forest-only rollout evidence is still required"
        )
    if not items["full_grid_load"]:
        blockers.append(f"full 1 ha grid is incomplete: {state['grid_cells']} of {contract_target_grid_cells}")
    if not items["gist_indexes"]:
        missing = [name for name in REQUIRED_GIST_INDEXES if not schema["indexes"].get(name, False)]
        blockers.append(f"missing GIST indexes: {', '.join(missing)}")
    if partitioning_required and not items["region_partitioning"]:
        missing = [
            name
            for name in REQUIRED_PARTITION_TARGETS
            if not _partition_ready(schema, name, acceptance_scope=acceptance_scope)
        ]
        blockers.append(f"missing region partitioning: {', '.join(missing)}")
    if not items["query_tuning"]:
        missing = [name for name in REQUIRED_QUERY_INDEXES if not schema["indexes"].get(name, False)]
        blockers.append(f"missing query tuning indexes: {', '.join(missing)}")
    if not performance_report.get("ready", False):
        blockers.append("performance report is missing or failed")
    if not items["frontend_performance_tests"]:
        blockers.append("frontend performance probes are incomplete")
    if not items["admin_performance_tests"]:
        blockers.append("admin performance probes are incomplete")
    return blockers


def _layer_a_load_ready(
    state: dict[str, Any],
    expected_layer_a_features: int,
    expected_ogm_tiles: int,
    acceptance_scope: str,
    public_current_source_proofs_ready: bool = False,
) -> bool:
    if acceptance_scope == "full-scale":
        return bool(
            _public_layer_a_source_ready(state)
            and (
                not expected_ogm_tiles
                or int(state["public_layer_a_total_tiles"] or 0) >= expected_ogm_tiles
            )
            and (not expected_layer_a_features or state["layer_a_features"] >= expected_layer_a_features)
            and int(state["layer_a_features"] or 0) > 0
            and state["osm_forest_features"] > 0
            and _nonforest_difference_ready(state)
        )
    return bool(
        _public_source_checks_ready(
            state,
            public_current_source_proofs_ready=public_current_source_proofs_ready,
        )
        and (
            not expected_ogm_tiles
            or int(state["public_layer_a_total_tiles"] or 0) >= expected_ogm_tiles
        )
    )


def _public_layer_a_source_ready(state: dict[str, Any]) -> bool:
    return bool(state["public_layer_a_completed_run"])


def _public_layer_a_feature_count(state: dict[str, Any], acceptance_scope: str) -> int:
    if acceptance_scope == "public-ogm":
        return int(state.get("layer_a_features") or 0)
    return int(state.get("layer_a_features") or 0)


def _public_source_checks_ready(
    state: dict[str, Any],
    public_current_source_proofs_ready: bool = False,
) -> bool:
    return bool(
        (_public_layer_a_source_ready(state) or public_current_source_proofs_ready)
        and int(state.get("layer_a_features") or 0) > 0
        and int(state.get("osm_forest_features") or 0) > 0
        and state.get("osm_forest_completed_check")
        and _nonforest_difference_ready(state)
    )


def _performance_expected_table_counts(state: dict[str, Any], acceptance_scope: str) -> dict[str, Any]:
    layer_a_count = (
        state.get("layer_a_features")
        if acceptance_scope == "public-ogm"
        else state.get("layer_a_features")
    )
    return {
        "grid_cells": state["grid_cells"],
        "layer_a_features": layer_a_count,
        "ndvi_observations": state["ndvi_observations"],
        "verification_results": state["verification_results"],
        "carbon_assessments": state["carbon_assessments"],
        "carbon_audit_events": state["carbon_audit_events"],
    }


def _partition_ready(schema: dict[str, Any], table_name: str, *, acceptance_scope: str) -> bool:
    if schema.get("region_partitions", {}).get(table_name):
        return True
    if acceptance_scope == "full-scale":
        return False
    return bool(
        schema.get("shadow_region_partitions", {}).get(table_name)
        and schema.get("shadow_partition_counts_match", {}).get(table_name)
    )


def _nonforest_difference_ready(state: dict[str, Any]) -> bool:
    has_component_state = any(
        key in state
        for key in (
            "osm_nonforest_polygon_features",
            "osm_nonforest_line_features",
            "osm_nonforest_polygon_completed_check",
            "osm_nonforest_line_completed_check",
        )
    )
    if not has_component_state:
        return bool(state.get("osm_nonforest_completed_check") and int(state.get("osm_nonforest_features") or 0) > 0)
    return bool(
        state.get("osm_nonforest_polygon_completed_check")
        and state.get("osm_nonforest_line_completed_check")
    )


def _progress(current: int, target: int) -> dict[str, Any]:
    current = int(current or 0)
    target = int(target or 0)
    if target <= 0:
        return {"current": current, "target": None, "ratio": None}
    return {"current": current, "target": target, "ratio": round(min(current / target, 1), 6)}


def _load_state(db, params: dict[str, Any]) -> dict[str, Any]:
    return dict(
        db.execute(
            text(
                """
                WITH target_region AS (
                    SELECT region.id, region.code, region.metadata
                    FROM regions region
                    WHERE region.code = :region_code
                ),
                source_scope AS (
                    SELECT COALESCE(
                        target_region.metadata->'forest_mask'->>'source_region_code',
                        target_region.code,
                        :region_code
                    ) AS source_region_code
                    FROM target_region
                    UNION ALL
                    SELECT :region_code
                    WHERE NOT EXISTS (SELECT 1 FROM target_region)
                    LIMIT 1
                ),
                layer_a_run_fragments_raw AS (
                    SELECT
                        run.id,
                        run.source,
                        run.status,
                        run.tile_size,
                        run.total_tiles,
                        run.processed_tiles,
                        run.bolme_inserted,
                        run.mescere_inserted,
                        run.failed_bolme,
                        run.failed_mescere,
                        run.bbox::text AS bbox,
                        run.started_at,
                        COALESCE((run.metadata->>'max_tiles')::integer, 0) AS max_tiles,
                        COALESCE((run.metadata->>'tile_shard_count')::integer, 1) AS shard_count,
                        COALESCE((run.metadata->>'tile_shard_index')::integer, 0) AS shard_index,
                        COALESCE(run.metadata->>'bbox_source', 'region') AS bbox_source,
                        COALESCE(run.metadata->>'region_geometry_hash', '') AS region_geometry_hash,
                        COALESCE(run.metadata->>'tile_intersect_source_layers', '[]') AS tile_intersect_source_layers
                    FROM stage3_layer_a_ingest_runs run
                    JOIN regions region ON region.id = run.region_id
                    CROSS JOIN source_scope
                    WHERE region.code = source_scope.source_region_code
                      AND COALESCE((run.metadata->>'max_tiles')::integer, 0) = 0
                      AND COALESCE(run.metadata->>'bbox_source', 'region') = 'region'
                      AND COALESCE((run.metadata->>'tile_shard_count')::integer, 1) >= 1
                ),
                layer_a_run_fragments AS (
                    SELECT DISTINCT ON (
                        source,
                        shard_count,
                        shard_index,
                        tile_size,
                        bbox,
                        region_geometry_hash,
                        tile_intersect_source_layers
                    )
                        *
                    FROM layer_a_run_fragments_raw
                    ORDER BY
                        source,
                        shard_count,
                        shard_index,
                        tile_size,
                        bbox,
                        region_geometry_hash,
                        tile_intersect_source_layers,
                        started_at DESC NULLS LAST,
                        id DESC
                ),
                layer_a_run_groups AS (
                    SELECT
                        source,
                        shard_count,
                        tile_size,
                        bbox,
                        region_geometry_hash,
                        tile_intersect_source_layers,
                        sum(total_tiles)::integer AS total_tiles,
                        sum(processed_tiles)::integer AS processed_tiles,
                        max(started_at) AS max_started_at,
                        count(DISTINCT shard_index)::integer AS distinct_shards,
                        bool_and(shard_index >= 0 AND shard_index < shard_count) AS valid_shard_indexes,
                        bool_and(
                            status = 'completed'
                            AND total_tiles > 0
                            AND processed_tiles >= total_tiles
                            AND failed_bolme = 0
                            AND failed_mescere = 0
                            AND (
                                (source = 'all' AND bolme_inserted > 0 AND mescere_inserted > 0)
                                OR (source = 'bolme' AND bolme_inserted > 0)
                                OR (source = 'mescere' AND mescere_inserted > 0)
                            )
                        ) AS fragments_completed
                    FROM layer_a_run_fragments
                    GROUP BY
                        source,
                        shard_count,
                        tile_size,
                        bbox,
                        region_geometry_hash,
                        tile_intersect_source_layers
                ),
                latest_layer_a_group AS (
                    SELECT *
                    FROM layer_a_run_groups
                    WHERE valid_shard_indexes
                    ORDER BY max_started_at DESC NULLS LAST
                    LIMIT 1
                ),
                latest_completed_layer_a_group AS (
                    SELECT *
                    FROM layer_a_run_groups
                    WHERE valid_shard_indexes
                      AND fragments_completed
                      AND distinct_shards = shard_count
                    ORDER BY max_started_at DESC NULLS LAST
                    LIMIT 1
                ),
                latest_grid_run AS (
                    SELECT run.*
                    FROM stage3_grid_generation_runs run
                    JOIN regions region ON region.id = run.region_id
                    WHERE region.code = :region_code
                      AND run.grid_version = :grid_version
                    ORDER BY run.started_at DESC, run.id DESC
                    LIMIT 1
                )
                SELECT
                    (
                        SELECT count(*)
                        FROM grid_cells cell
                        JOIN regions region ON region.id = cell.region_id
                        WHERE region.code = :region_code
                          AND cell.grid_version = :grid_version
                    ) AS grid_cells,
                    (
                        SELECT count(*)
                        FROM ogm_parcels parcel
                        CROSS JOIN source_scope
                        WHERE parcel.is_current
                          AND parcel.source_layer = ANY(:layer_a_source_layers)
                          AND parcel.attributes->>'region_code' = source_scope.source_region_code
                    ) AS layer_a_features,
                    (
                        SELECT count(*)
                        FROM ogm_parcels parcel
                        CROSS JOIN source_scope
                        WHERE parcel.is_current
                          AND parcel.source_layer = 'osm_forest_mask'
                          AND parcel.attributes->>'region_code' = source_scope.source_region_code
                    ) AS osm_forest_features,
                    (
                        SELECT count(*)
                        FROM ogm_parcels parcel
                        CROSS JOIN source_scope
                        WHERE parcel.is_current
                          AND parcel.source_layer = 'osm_nonforest_exclusion'
                          AND parcel.attributes->>'region_code' = source_scope.source_region_code
                    ) AS osm_nonforest_features,
                    (
                        SELECT count(*)
                        FROM ogm_parcels parcel
                        CROSS JOIN source_scope
                        WHERE parcel.is_current
                          AND parcel.source_layer = 'osm_nonforest_exclusion'
                          AND parcel.attributes->>'region_code' = source_scope.source_region_code
                          AND parcel.attributes->>'exclusion_component' = 'polygon'
                    ) AS osm_nonforest_polygon_features,
                    (
                        SELECT count(*)
                        FROM ogm_parcels parcel
                        CROSS JOIN source_scope
                        WHERE parcel.is_current
                          AND parcel.source_layer = 'osm_nonforest_exclusion'
                          AND parcel.attributes->>'region_code' = source_scope.source_region_code
                          AND parcel.attributes->>'exclusion_component' = 'line'
                    ) AS osm_nonforest_line_features,
                    (
                        SELECT count(*)
                        FROM sentinel_observations observation
                        JOIN grid_cells cell ON cell.id = observation.grid_cell_id
                        JOIN regions region ON region.id = cell.region_id
                        WHERE observation.is_current
                          AND observation.ndvi IS NOT NULL
                          AND observation.region_code = :region_code
                          AND observation.grid_version = :grid_version
                          AND region.code = :region_code
                          AND cell.grid_version = :grid_version
                    ) AS ndvi_observations,
                    (
                        SELECT count(*)
                        FROM verification_results result
                        JOIN grid_cells cell ON cell.id = result.grid_cell_id
                        JOIN regions region ON region.id = cell.region_id
                        WHERE result.is_current
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                          AND region.code = :region_code
                          AND cell.grid_version = :grid_version
                    ) AS verification_results,
                    (
                        SELECT count(*)
                        FROM carbon_assessments assessment
                        JOIN grid_cells cell ON cell.id = assessment.grid_cell_id
                        JOIN regions region ON region.id = cell.region_id
                        WHERE assessment.is_current
                          AND region.code = :region_code
                          AND cell.grid_version = :grid_version
                    ) AS carbon_assessments,
                    (
                        SELECT count(*)
                        FROM carbon_assessment_events event
                        JOIN grid_cells cell ON cell.id = event.grid_cell_id
                        JOIN regions region ON region.id = cell.region_id
                        WHERE region.code = :region_code
                          AND cell.grid_version = :grid_version
                    ) AS carbon_audit_events,
                    (
                        SELECT COALESCE(region.metadata->>'forest_only', 'false') = 'true'
                        FROM regions region
                        WHERE region.code = :region_code
                    ) AS forest_only_region,
                    (
                        SELECT COALESCE(latest_grid_run.metadata->>'forest_only', 'false') = 'true'
                        FROM latest_grid_run
                    ) AS forest_only_grid_run,
                    (
                        SELECT EXISTS (SELECT 1 FROM latest_completed_layer_a_group)
                    ) AS public_layer_a_completed_run,
                    (
                        SELECT COALESCE((SELECT total_tiles FROM latest_layer_a_group), 0)
                    ) AS public_layer_a_total_tiles,
                    (
                        SELECT COALESCE((SELECT processed_tiles FROM latest_layer_a_group), 0)
                    ) AS public_layer_a_processed_tiles,
                    (
                        SELECT COALESCE(checks.status = 'ok' AND checks.feature_count > 0, false)
                        FROM source_checks checks
                        CROSS JOIN source_scope
                        WHERE checks.region_code = source_scope.source_region_code
                          AND checks.source_layer = 'osm_forest_mask'
                          AND checks.source_type = 'osm_pbf_geofabrik'
                        ORDER BY checks.checked_at DESC, checks.id DESC
                        LIMIT 1
                    ) AS osm_forest_completed_check,
                    (
                        SELECT COALESCE(checks.status = 'ok' AND checks.feature_count > 0, false)
                        FROM source_checks checks
                        CROSS JOIN source_scope
                        WHERE checks.region_code = source_scope.source_region_code
                          AND checks.source_layer = 'osm_nonforest_exclusion'
                          AND checks.source_type = 'osm_pbf_geofabrik'
                        ORDER BY checks.checked_at DESC, checks.id DESC
                        LIMIT 1
                    ) AS osm_nonforest_completed_check,
                    (
                        SELECT EXISTS (
                            SELECT 1
                            FROM source_checks checks
                            CROSS JOIN source_scope
                            WHERE checks.region_code = source_scope.source_region_code
                              AND checks.source_layer = 'osm_nonforest_exclusion'
                              AND checks.source_type = 'osm_pbf_geofabrik'
                          AND checks.status IN ('ok', 'empty')
                          AND (
                              checks.metadata->>'components' IN ('all', 'nonforest', 'nonforest-polygons')
                              OR checks.metadata->>'component' = 'polygon'
                          )
                          AND checks.metadata ? 'stored_polygon_features'
                    )
                ) AS osm_nonforest_polygon_completed_check,
                (
                        SELECT EXISTS (
                            SELECT 1
                            FROM source_checks checks
                            CROSS JOIN source_scope
                            WHERE checks.region_code = source_scope.source_region_code
                              AND checks.source_layer = 'osm_nonforest_exclusion'
                              AND checks.source_type = 'osm_pbf_geofabrik'
                          AND checks.status IN ('ok', 'empty')
                          AND (
                              checks.metadata->>'components' IN ('all', 'nonforest', 'nonforest-lines')
                              OR checks.metadata->>'component' = 'line'
                          )
                          AND checks.metadata ? 'stored_line_features'
                    )
                ) AS osm_nonforest_line_completed_check
                """
            ),
            params,
        ).mappings().one()
    )


def _load_schema(db, region_code: str = "") -> dict[str, Any]:
    index_rows = db.execute(
        text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = ANY(:index_names)
            """
        ),
        {"index_names": list(REQUIRED_GIST_INDEXES + REQUIRED_QUERY_INDEXES)},
    ).scalars().all()
    partition_rows = db.execute(
        text(
            """
            SELECT
                cls.relname,
                pg_get_partkeydef(cls.oid) AS partition_key
            FROM pg_partitioned_table partitioned
            JOIN pg_class cls ON cls.oid = partitioned.partrelid
            JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
            WHERE namespace.nspname = 'public'
              AND cls.relname = ANY(:table_names)
            """
        ),
        {"table_names": list(REQUIRED_PARTITION_TARGETS)},
    ).mappings().all()
    indexes = {name: name in index_rows for name in REQUIRED_GIST_INDEXES + REQUIRED_QUERY_INDEXES}
    region_partitions = {
        name: any(row["relname"] == name and "region" in (row["partition_key"] or "").lower() for row in partition_rows)
        for name in REQUIRED_PARTITION_TARGETS
    }
    shadow_region_partitions = _load_shadow_partition_status(db)
    shadow_partition_counts_match = _load_shadow_partition_validation(db, region_code)
    return {
        "indexes": indexes,
        "region_partitions": region_partitions,
        "shadow_region_partitions": shadow_region_partitions,
        "shadow_partition_counts_match": shadow_partition_counts_match,
        "layer_a_ingest_tracking": _table_exists(db, "stage3_layer_a_ingest_runs")
        and _table_exists(db, "stage3_layer_a_ingest_tiles"),
        "source_adapter_boundary": _source_adapter_boundary_ready(),
    }


def _table_exists(db, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _load_shadow_partition_status(db) -> dict[str, bool]:
    shadow_names = [f"{name}_partitioned" for name in REQUIRED_PARTITION_TARGETS]
    rows = db.execute(
        text(
            """
            SELECT
                cls.relname,
                lower(pg_get_partkeydef(cls.oid)) AS partition_key
            FROM pg_partitioned_table partitioned
            JOIN pg_class cls ON cls.oid = partitioned.partrelid
            JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
            WHERE namespace.nspname = 'public'
              AND cls.relname = ANY(:names)
            """
        ),
        {"names": shadow_names},
    ).mappings().all()
    return {
        name: any(
            row["relname"] == f"{name}_partitioned"
            and "region" in (row["partition_key"] or "").lower()
            for row in rows
        )
        for name in REQUIRED_PARTITION_TARGETS
    }


def _load_shadow_partition_validation(db, region_code: str = "") -> dict[str, bool]:
    validation: dict[str, bool] = {}
    region_ids = _region_ids(db, region_code)
    for table_name in REQUIRED_PARTITION_TARGETS:
        shadow_name = f"{table_name}_partitioned"
        if not _table_exists(db, table_name) or not _table_exists(db, shadow_name):
            validation[table_name] = False
            continue
        validation[table_name] = _count_table(db, table_name, region_ids) == _count_table(db, shadow_name, region_ids)
    return validation


def _region_ids(db, region_code: str) -> list[int]:
    if not region_code:
        return []
    rows = db.execute(
        text("SELECT id FROM regions WHERE code = :region_code"),
        {"region_code": region_code},
    ).scalars().all()
    return [int(row) for row in rows]


def _count_table(db, table_name: str, region_ids: list[int] | None = None) -> int:
    if table_name not in REQUIRED_PARTITION_TARGETS and not table_name.endswith("_partitioned"):
        raise ValueError(f"unsupported table: {table_name}")
    if region_ids:
        if table_name.endswith("_partitioned") or table_name == "grid_cells":
            return int(
                db.execute(
                    text(f"SELECT count(*) FROM {table_name} WHERE region_id = ANY(:region_ids)"),
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
    return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _source_adapter_boundary_ready() -> bool:
    strategy = stage1_source_strategy()
    extension = strategy.get("extension_model") or {}
    future_sources = extension.get("future_adapter_sources") or []
    return (
        extension.get("integration_boundary") == "normalized Layer A records"
        and any(
            source.get("source_quality") == "official_full_layer_a"
            and source.get("expected_input")
            for source in future_sources
        )
    )


if __name__ == "__main__":
    main()
