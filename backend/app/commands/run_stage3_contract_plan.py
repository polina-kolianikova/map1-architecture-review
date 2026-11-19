from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.api.stage1 import stage1_params
from app.db.session import SessionLocal
from app.services.source_strategy import SOURCE_LAYER_BOLME, SOURCE_LAYER_MESCERE
from app.services.stage3_grid_options import projected_srid_for_lon_lat
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS,
    STAGE3_PUBLIC_V9_FORMULA,
    STAGE3_PUBLIC_V9_GRID_SIZE_M,
    STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS,
    STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA,
    STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG,
    STAGE3_PUBLIC_V9_SOURCE_LAYERS,
    stage3_public_v9_manifest_hash,
)


DEFAULT_TARGET_HECTARES = 23_000_000
DEFAULT_GRID_SIZE_M = STAGE3_PUBLIC_V9_GRID_SIZE_M
DEFAULT_GRID_TILE_SIZE_M = 10_000
DEFAULT_OGM_TILE_SIZE_DEG = 0.12
DEFAULT_GRID_BATCH_CELLS = 250_000
DEFAULT_NDVI_BATCH_CELLS = 1_000
DEFAULT_FULL_SCALE_PROJECTED_SRID = 6933
DEFAULT_MIN_CLEAN_MASK_COMPONENT_AREA_HA = STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA
TURKEY_BBOX = (25.0, 35.5, 45.2, 42.5)


def _layer_arg(flag: str, layers: tuple[str, ...]) -> str:
    if not layers:
        return ""
    return f"{flag} {','.join(layers)} "


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Stage 3 contract execution plan")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", required=True)
    parser.add_argument("--target-hectares", type=float, default=DEFAULT_TARGET_HECTARES)
    parser.add_argument("--grid-size-m", type=int, default=DEFAULT_GRID_SIZE_M)
    parser.add_argument("--grid-tile-size-m", type=int, default=DEFAULT_GRID_TILE_SIZE_M)
    parser.add_argument("--ogm-tile-size-deg", type=float, default=DEFAULT_OGM_TILE_SIZE_DEG)
    parser.add_argument("--grid-batch-cells", type=int, default=DEFAULT_GRID_BATCH_CELLS)
    parser.add_argument("--full-scale-projected-srid", type=int, default=DEFAULT_FULL_SCALE_PROJECTED_SRID)
    parser.add_argument(
        "--min-clean-mask-component-area-ha",
        type=float,
        default=DEFAULT_MIN_CLEAN_MASK_COMPONENT_AREA_HA,
    )
    parser.add_argument("--expected-layer-a-features", type=int, default=0)
    parser.add_argument("--acceptance-scope", choices=("public-ogm", "full-scale"), default="public-ogm")
    parser.add_argument(
        "--ack-full-turkey-user-approved",
        action="store_true",
        help="include the guarded full Turkey V10 approval flag in generated full-run commands",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    _validate_args(args)

    with SessionLocal() as db:
        params = stage1_params(region_code=args.region_code, grid_version=args.grid_version, db=db)
        region = _load_region(db, args.region_code)
        current = _load_current_counts(db, params, region)
        schema = _load_schema_status(db)

    projected_srid = _selected_projected_srid(args, region, current)
    area_ha = region["area_ha"]
    estimated_grid_cells_upper_bound = estimate_grid_cells(area_ha, args.grid_size_m)
    target_grid_cells = estimate_grid_cells(args.target_hectares, args.grid_size_m)
    estimated_grid_cells = _expected_region_grid_cells(
        current=current,
        region=region,
        area_based_estimate=estimated_grid_cells_upper_bound,
        acceptance_scope=args.acceptance_scope,
        full_scale_target=target_grid_cells,
    )
    if args.acceptance_scope == "full-scale":
        estimated_grid_cells = target_grid_cells
    min_clean_mask_component_area_ha = _min_clean_mask_component_area_ha(args)
    grid_tile_count = _selected_grid_tile_count(args, region, current, target_grid_cells)
    ogm_tile_count = estimate_ogm_tiles(
        region["min_lon"],
        region["min_lat"],
        region["max_lon"],
        region["max_lat"],
        args.ogm_tile_size_deg,
    )
    if args.acceptance_scope == "full-scale":
        ogm_tile_count = estimate_ogm_tiles(*TURKEY_BBOX, args.ogm_tile_size_deg)
    grid_batches = estimate_batches(estimated_grid_cells, args.grid_batch_cells)
    target_batches = estimate_batches(target_grid_cells, args.grid_batch_cells)
    report = {
        "stage": "stage3",
        "contract_scope": {
            "accepted_stage3_scope": (
                "forest_only_public_ogm_osm_full_scale_pipeline"
                if args.acceptance_scope == "full-scale"
                else "forest_only_public_ogm_osm_pipeline"
            ),
            "public_layer_a_pipeline": True,
            "public_full_scale_path": True,
            "forest_only_grid_scope": True,
            "raw_cadastre_grid_load": False,
            "official_full_layer_a_status": "backlog",
            "future_layer_a_source_adapter": True,
            "full_layer_a_requires_official_source": False,
            "client_mask_formula": STAGE3_PUBLIC_V9_FORMULA,
            "formula_manifest_hash": stage3_public_v9_manifest_hash(),
            "primary_source_layers": list(STAGE3_PUBLIC_V9_SOURCE_LAYERS),
            "intersect_source_layers": list(STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS),
            "difference_source_layers": list(STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS),
            "isolated_ogm_parser": True,
            "target_hectares": args.target_hectares,
            "future_target_grid_cells": target_grid_cells,
            "grid_size_m": args.grid_size_m,
            "grid_projection_strategy": _grid_projection_strategy(args, current),
            "postgis_scale_target": "region partitions, spatial indexes, query baselines",
            "clean_mask_component_min_area_ha": min_clean_mask_component_area_ha,
        },
        "region": {
            "code": region["code"],
            "name": region["name"],
            "country_code": region["country_code"],
            "area_ha": round(area_ha, 2),
            "forest_only": bool(region.get("forest_only")),
            "region_source": region.get("region_source"),
            "source_region_code": region.get("source_region_code"),
            "bbox": [
                round(region["min_lon"], 6),
                round(region["min_lat"], 6),
                round(region["max_lon"], 6),
                round(region["max_lat"], 6),
            ],
            "projected_srid": projected_srid,
        },
        "current_state": current,
        "estimates": {
            "region_grid_cells": estimated_grid_cells,
            "region_grid_cells_upper_bound": estimated_grid_cells_upper_bound,
            "target_grid_cells": target_grid_cells,
            "grid_tiles": grid_tile_count,
            "ogm_bbox_tiles": ogm_tile_count,
            "grid_batches": grid_batches,
            "target_grid_batches": target_batches,
        },
        "commands": _commands(args, projected_srid, estimated_grid_cells, params["layer_a_source_layers"], region),
        "execution_phases": execution_phases(
            grid_batches=grid_batches,
            target_grid_batches=target_batches,
            grid_tile_count=grid_tile_count,
            ogm_tile_count=ogm_tile_count,
        ),
        "schema": schema,
        "readiness_notes": readiness_notes(
            current=current,
            schema=schema,
            estimated_grid_cells=estimated_grid_cells,
            expected_layer_a_features=args.expected_layer_a_features,
        ),
    }
    payload = json.dumps(report, indent=2, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)


def estimate_grid_cells(area_ha: float, grid_size_m: int) -> int:
    cell_area_ha = (grid_size_m * grid_size_m) / 10_000.0
    return int(math.ceil(area_ha / cell_area_ha))


def _expected_region_grid_cells(
    *,
    current: dict[str, int],
    region: dict[str, Any],
    area_based_estimate: int,
    acceptance_scope: str,
    full_scale_target: int,
) -> int:
    if acceptance_scope == "full-scale":
        return full_scale_target
    if (
        acceptance_scope == "public-ogm"
        and full_scale_target >= 1_000_000
        and not region.get("forest_only")
    ):
        return full_scale_target
    if (
        acceptance_scope == "public-ogm"
        and region.get("forest_only")
        and current.get("latest_completed_grid_cells", 0) > 0
    ):
        return current["latest_completed_grid_cells"]
    return area_based_estimate


def estimate_batches(total_items: int, batch_size: int) -> int:
    return int(math.ceil(total_items / batch_size)) if total_items > 0 else 0


def estimate_grid_tiles(width_m: float, height_m: float, tile_size_m: int) -> int:
    return max(1, int(math.ceil(width_m / tile_size_m)) * int(math.ceil(height_m / tile_size_m)))


def estimate_grid_tiles_from_cell_target(total_cells: int, grid_size_m: int, tile_size_m: int) -> int:
    cells_per_tile_side = max(1, int(tile_size_m / grid_size_m))
    cells_per_tile = max(1, cells_per_tile_side * cells_per_tile_side)
    return max(1, int(math.ceil(total_cells / cells_per_tile)))


def estimate_ogm_tiles(min_lon: float, min_lat: float, max_lon: float, max_lat: float, tile_size_deg: float) -> int:
    epsilon = 1e-9
    lon_tiles = int(math.ceil(max(0.0, max_lon - min_lon) / tile_size_deg - epsilon))
    lat_tiles = int(math.ceil(max(0.0, max_lat - min_lat) / tile_size_deg - epsilon))
    return max(1, lon_tiles * lat_tiles)


def readiness_notes(
    current: dict[str, int],
    schema: dict[str, Any],
    estimated_grid_cells: int,
    expected_layer_a_features: int,
) -> list[str]:
    notes: list[str] = []
    if current["grid_cells"] < estimated_grid_cells:
        notes.append("full region grid has not been generated for the selected grid version")
    if expected_layer_a_features and current["layer_a_features"] < expected_layer_a_features:
        notes.append("Layer A feature count is below the supplied target")
    if not schema["grid_generation_tracking"]:
        notes.append("grid generation run tracking tables are missing")
    if not schema["layer_a_ingest_tracking"]:
        notes.append("Layer A ingest run tracking tables are missing")
    if not schema["core_spatial_indexes"]:
        notes.append("core spatial indexes are missing")
    if estimated_grid_cells >= 1_000_000 and not schema["partitioning_detected"]:
        notes.append("large grid target requires a partition rollout before production-scale loading")
    if not notes:
        notes.append("Stage 3 planning checks are ready for the selected scope")
    return notes


def execution_phases(
    grid_batches: int,
    target_grid_batches: int,
    grid_tile_count: int,
    ogm_tile_count: int,
) -> list[dict[str, Any]]:
    return [
        {
            "name": "source_access_check",
            "goal": "confirm OGM REST/WMS access through the configured network path",
            "exit_criteria": ["BOLME endpoint returns JSON", "MESCERE WMS returns capabilities XML"],
        },
        {
            "name": "region_seed",
            "goal": "prepare a coarse bbox or WKT seed only for source access and tiling",
            "exit_criteria": ["region exists", "region area guard passes", "bbox/WKT source is recorded"],
        },
        {
            "name": "layer_a_ingest",
            "goal": "fetch and normalize OGM Layer A by bbox tiles or import an official vector export",
            "estimated_tiles": ogm_tile_count,
            "exit_criteria": ["all OGM tiles completed", "no repeated source failures", "source checks are ok"],
        },
        {
            "name": "osm_forest_mask",
            "goal": "load public OSM forest polygons for the same workspace",
            "exit_criteria": ["OSM forest polygons exist", "source check is recorded", "geometry is clipped to the seed region"],
        },
        {
            "name": "osm_nonforest_exclusion",
            "goal": "load public OSM roads, buildings, and field polygons for forest-mask difference",
            "exit_criteria": [
                "OSM exclusion geometries exist",
                "source check is recorded",
                "exclusion geometries are clipped to the seed region",
            ],
        },
        {
            "name": "forest_region_refine",
            "goal": "replace the coarse seed with dissolved public OGM plus public OSM forest mask after OSM non-forest difference",
            "exit_criteria": ["Layer A forest geometries exist", "forest-only metadata is recorded", "forest-region area guard passes"],
        },
        {
            "name": "grid_plan",
            "goal": "build the tile queue and estimate grid volume before insert",
            "estimated_tiles": grid_tile_count,
            "estimated_batches": grid_batches,
            "target_batches": target_grid_batches,
            "exit_criteria": ["dry-run tile count reviewed", "projected SRID selected", "no existing run conflict"],
        },
        {
            "name": "grid_load",
            "goal": "load 1 ha cells in resumable tiles inside the forest-only geometry",
            "exit_criteria": ["all grid tiles completed", "inserted cell count matches target tolerance", "no failed tiles"],
        },
        {
            "name": "forest_clip_check",
            "goal": "prove the generated cells are inside the final forest mask",
            "exit_criteria": ["no cells outside mask", "cell geometry is valid", "mask metadata includes OSM refinement"],
        },
        {
            "name": "clean_mask_integrity",
            "goal": "prove the final clean mask equals the client formula: public OGM plus OSM forest minus OSM non-forest",
            "exit_criteria": [
                "final components do not intersect non-forest exclusions",
                "final components do not extend outside public OGM ∪ OSM forest",
                "source cache is current",
            ],
        },
        {
            "name": "mask_formula_integrity",
            "goal": "prove the persisted final components match the union formula without losing OGM-only or OSM-only public forest area",
            "exit_criteria": [
                "formula-aware symmetric difference passes",
                "OGM-only and OSM-only public forest areas are included unless removed by non-forest exclusion or <1 ha cleanup",
                "only final connected components below the clean-mask threshold are removed",
            ],
        },
        {
            "name": "grid_spatial_integrity",
            "goal": "prove the persisted grid covers only the final clean mask without gaps or overlaps",
            "exit_criteria": ["no mask area missing cells", "no cells outside mask", "no positive cell overlap"],
        },
        {
            "name": "postgis_optimization",
            "goal": "validate spatial indexes, partitioning, and query plans",
            "exit_criteria": ["GIST indexes present", "large tables partitioned when target exceeds 1M cells", "ANALYZE completed"],
        },
        {
            "name": "acceptance_report",
            "goal": "produce readiness and performance reports for Stage 3 acceptance",
            "exit_criteria": [
                "readiness has no blockers",
                "performance baseline is within accepted thresholds",
                "contract audit maps each Stage 3 item to evidence or blocker",
            ],
        },
    ]


def _commands(
    args,
    projected_srid: int,
    region_grid_cells: int | None = None,
    verification_source_layers: list[str] | None = None,
    region: dict[str, Any] | None = None,
) -> dict[str, str]:
    performance_output = "stage3-performance-report.json"
    readiness_output = "stage3-readiness-report.json"
    forest_clip_output = "stage3-forest-clip-report.json"
    grid_mask_proof_output = "stage3-grid-mask-proof-report.json"
    clean_mask_integrity_output = "stage3-fragment-formula-equivalence-report.json"
    grid_spatial_integrity_output = "stage3-grid-spatial-integrity-report.json"
    bounded_public_scope = getattr(args, "acceptance_scope", "public-ogm") == "public-ogm"
    contract_target_grid_cells = estimate_grid_cells(args.target_hectares, args.grid_size_m)
    grid_batch_cells = getattr(args, "grid_batch_cells", DEFAULT_GRID_BATCH_CELLS)
    min_clean_mask_component_area_ha = _min_clean_mask_component_area_ha(args)
    component_mask_grid_flag = "--use-region-mask-components "
    full_approval_arg = _full_approval_arg(args)
    expected_grid_cells = region_grid_cells if bounded_public_scope and region_grid_cells is not None else estimate_grid_cells(
        args.target_hectares,
        args.grid_size_m,
    )
    expected_ogm_tiles = (
        estimate_ogm_tiles(*TURKEY_BBOX, args.ogm_tile_size_deg)
        if getattr(args, "acceptance_scope", "public-ogm") == "full-scale"
        else
        estimate_ogm_tiles(region["min_lon"], region["min_lat"], region["max_lon"], region["max_lat"], args.ogm_tile_size_deg)
        if region
        else 0
    )
    source_region_code = str(region.get("source_region_code") if region else args.region_code) or args.region_code
    seed_region = _seed_region_command(args, bounded_public_scope, region, source_region_code=source_region_code)
    forest_region = _forest_region_command(
        args,
        bounded_public_scope,
        region,
        source_region_code=source_region_code,
        projected_srid=projected_srid,
    )
    base = (
        "python -m app.commands.run_stage3_grid_generation_batched "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        f"--grid-size-m {args.grid_size_m} "
        f"--projected-srid {projected_srid} "
        f"--tile-size-m {args.grid_tile_size_m} "
        f"--min-mask-component-area-ha {min_clean_mask_component_area_ha} "
        f"{component_mask_grid_flag}"
        f"{full_approval_arg}"
        "--max-failed-tiles 0"
    )
    layer_a = (
        "python -m app.commands.run_stage3_layer_a_ingest "
        f"--region-code {source_region_code} "
        "--bbox-from-region "
        f"--tile-size {args.ogm_tile_size_deg} "
        "--source all "
        "--source-retries 4 "
        "--retry-delay 5 "
        "--subdivide-failed-tiles 2 "
        "--retire-missing-bolme "
        "--retire-missing-mescere"
    )
    official_layer_a_import = (
        "python -m app.commands.run_stage3_official_layer_a_import "
        f"--region-code {source_region_code} "
        "--file <official-layer-a.geojson> "
        "--source-id-property <parcel-id-property>"
    )
    osm_forest_mask = (
        "python -m app.commands.ingest_osm_forest_mask "
        f"--region-code {source_region_code} "
        "--bbox-from-region"
    )
    osm_nonforest_exclusion = (
        "python -m app.commands.ingest_osm_nonforest_exclusion "
        f"--region-code {source_region_code} "
        "--bbox-from-region"
    )
    worldcover_nonforest_exclusion = (
        "# Run with the schema-owner database role because GDAL creates a staging table.\n"
        "python -m app.commands.ingest_worldcover_nonforest_exclusion "
        f"--region-code {source_region_code} "
        f"--projected-srid {projected_srid}"
    )
    osm_bulk_masks = (
        "# Run with the schema-owner database role because GDAL creates staging tables.\n"
        "python -m app.commands.ingest_osm_pbf_masks "
        f"--region-code {source_region_code} "
        "--mode all "
        "--pbf-url https://download.geofabrik.de/europe/turkey-latest.osm.pbf "
        "--force-download "
        "--min-forest-area-ha 0 "
        "--min-exclusion-area-ha 0 "
        "--nonforest-filter-source-layers ogm_esatis_bolme,osm_forest_mask "
        f"--projected-srid {projected_srid}"
    )
    osm_bulk_forest = osm_bulk_masks.replace("--mode all", "--mode forest")
    osm_bulk_nonforest_polygons = osm_bulk_masks.replace("--mode all", "--mode nonforest-polygons")
    osm_bulk_nonforest_lines = osm_bulk_masks.replace("--mode all", "--mode nonforest-lines")
    source_mask_cache = (
        "python -m app.commands.prepare_stage3_source_mask_cache "
        f"--source-region-code {source_region_code} "
        "--source-layers ogm_esatis_bolme,osm_forest_mask,osm_nonforest_exclusion "
        f"--projected-srid {projected_srid} "
        "--replace --analyze "
        f"{full_approval_arg}".rstrip()
    )
    readiness = (
        "python -m app.commands.run_stage3_readiness "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        f"--acceptance-scope {args.acceptance_scope} "
        f"--expected-grid-cells {expected_grid_cells} "
        f"--expected-layer-a-features {args.expected_layer_a_features} "
        f"--performance-report {performance_output} "
        f"--forest-clip-report {forest_clip_output} "
        f"--grid-mask-proof-report {grid_mask_proof_output} "
        f"--clean-mask-integrity-report {clean_mask_integrity_output} "
        f"--grid-spatial-integrity-report {grid_spatial_integrity_output} "
        f"--output {readiness_output} "
        "--fail-on-not-ready"
    )
    contract_audit = (
        "python -m app.commands.run_stage3_contract_audit "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        f"--acceptance-scope {args.acceptance_scope} "
        f"--target-hectares {args.target_hectares} "
        f"--grid-size-m {args.grid_size_m} "
        f"--expected-grid-cells {expected_grid_cells} "
        f"--expected-layer-a-features {args.expected_layer_a_features} "
        f"--expected-ogm-tiles {expected_ogm_tiles} "
        f"--performance-report {performance_output} "
        f"--forest-clip-report {forest_clip_output} "
        f"--grid-mask-proof-report {grid_mask_proof_output} "
        f"--clean-mask-integrity-report {clean_mask_integrity_output} "
        f"--grid-spatial-integrity-report {grid_spatial_integrity_output} "
        "--fail-on-not-ready"
    )
    performance = (
        "python -m app.commands.run_stage3_performance_baseline "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        "--iterations 5 --limit 200 "
        "--max-avg-ms 250 --max-p95-ms 500 "
        "--allow-staged-evidence "
        f"--explain --output {performance_output}"
    )
    scale_hardening = (
        "# Run with the schema-owner database role, not the runtime app role.\n"
        "python -m app.commands.run_stage3_scale_hardening "
        "--apply-indexes "
        "--analyze "
        f"--acceptance-scope {args.acceptance_scope} "
        f"{'--require-partitions ' if _partitions_required(contract_target_grid_cells) else ''}"
        f"{'--allow-shadow-partitions ' if args.acceptance_scope == 'public-ogm' else ''}"
        "--output stage3-scale-hardening-report.json"
    )
    partition_rollout = (
        "# Apply with the schema-owner database role before full-scale loading.\n"
        "python -m app.commands.run_stage3_partition_rollout "
        f"--region-code {args.region_code} "
        "--allow-online-shadow "
        "--apply-shadow "
        "--emit-sql "
        "--output stage3-partition-rollout-plan.json"
    )
    forest_clip = (
        "python -m app.commands.check_stage3_forest_clip "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        f"--output {forest_clip_output}"
    )
    grid_mask_proof = (
        "python -m app.commands.check_stage3_grid_mask_proof "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        f"--mask-version {args.region_code} "
        f"--projected-srid {projected_srid} "
        "--min-component-area-ha 1.0 "
        "--max-area-gap-ha 2.0 "
        f"--output {grid_mask_proof_output}"
    )
    clean_mask_integrity = (
        "python scripts/stage3_v9_fragment_equivalence_gate.py "
        f"--region-code {args.region_code} "
        f"--source-region-code {source_region_code} "
        f"--clip-region-code {source_region_code} "
        f"--mask-version {args.region_code} "
        f"{_layer_arg('--source-layers', STAGE3_PUBLIC_V9_SOURCE_LAYERS)}"
        f"{_layer_arg('--intersect-source-layers', STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS)}"
        f"{_layer_arg('--exclusion-source-layers', STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS)}"
        f"--projected-srid {projected_srid} "
        "--tile-size-m 10000 "
        "--snap-grid-m 0 "
        "--subdivide-max-vertices 256 "
        "--min-component-area-ha 1.0 "
        "--max-symdiff-area-m2 1.0 "
        "--max-exclusion-overlap-m2 1.0 "
        "--max-outside-region-m2 1.0 "
        f"--output {clean_mask_integrity_output}"
    )
    grid_spatial_integrity = (
        "python -m app.commands.check_stage3_grid_spatial_integrity "
        f"--region-code {args.region_code} "
        f"--grid-version {args.grid_version} "
        f"--mask-version {args.region_code} "
        f"--projected-srid {projected_srid} "
        f"--output {grid_spatial_integrity_output}"
    )
    return {
        "seed_region": seed_region,
        "layer_a_plan": f"{layer_a} --plan-only",
        "layer_a_run": layer_a,
        "official_layer_a_import": official_layer_a_import,
        "osm_bulk_masks_run": osm_bulk_masks,
        "osm_bulk_forest_run": osm_bulk_forest,
        "osm_bulk_nonforest_polygons_run": osm_bulk_nonforest_polygons,
        "osm_bulk_nonforest_lines_run": osm_bulk_nonforest_lines,
        "osm_forest_mask_run": osm_forest_mask,
        "osm_nonforest_exclusion_run": osm_nonforest_exclusion,
        "worldcover_nonforest_exclusion_run": worldcover_nonforest_exclusion,
        "source_mask_cache_prepare": source_mask_cache,
        "forest_region_from_layer_a": forest_region,
        "grid_plan": f"{base} --dry-run",
        "grid_run": base,
        "grid_adopt_existing": f"{base} --adopt-existing",
        "grid_resume": f"{base} --resume",
        "forest_clip_check": forest_clip,
        "grid_mask_proof": grid_mask_proof,
        "clean_mask_integrity": clean_mask_integrity,
        "grid_spatial_integrity": grid_spatial_integrity,
        "scale_hardening": scale_hardening,
        "partition_rollout_plan": partition_rollout,
        "performance_report": performance,
        "readiness_report": readiness,
        "contract_audit": contract_audit,
    }


def _seed_region_command(
    args,
    public_scope: bool,
    region: dict[str, Any] | None,
    *,
    source_region_code: str | None = None,
) -> str:
    region_code = source_region_code or args.region_code
    full_approval_arg = _full_approval_arg(args)
    command = (
        "python -m app.commands.run_stage3_prepare_region "
        f"--region-code {region_code} "
        f"--region-name {region_code} "
    )
    if public_scope and region:
        bbox = ",".join(
            _format_coord(region[key])
            for key in ("min_lon", "min_lat", "max_lon", "max_lat")
        )
        return f"{command}--bbox {bbox} {_area_guard_args(region['area_ha'])} {full_approval_arg}".rstrip()
    return (
        f"{command}--turkey-bbox --min-area-ha 20000000 --max-area-ha 150000000 "
        f"{full_approval_arg}"
    ).rstrip()


def _forest_region_command(
    args,
    public_scope: bool,
    region: dict[str, Any] | None,
    *,
    source_region_code: str | None = None,
    projected_srid: int,
) -> str:
    command = (
        "python -m app.commands.run_stage3_prepare_region_fragments "
        f"--region-code {args.region_code} "
        f"--region-name {args.region_code} "
    )
    source_region = f"--source-region-code {source_region_code} " if source_region_code else ""
    source_layers = _layer_arg("--source-layers", STAGE3_PUBLIC_V9_SOURCE_LAYERS)
    intersect_layers = _layer_arg("--intersect-mask-source-layers", STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS)
    osm_difference = _layer_arg("--difference-mask-source-layers", STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS)
    min_clean_mask_component_area_ha = _min_clean_mask_component_area_ha(args)
    base_options = (
        f"--projected-srid {projected_srid} "
        "--tile-size-m 10000 "
        "--snap-grid-m 0 "
        "--edge-tolerance-m 0.02 "
        "--subdivide-max-vertices 256 "
    )
    full_approval_arg = _full_approval_arg(args)
    if public_scope and region:
        return (
            f"{command}{source_region}{source_layers}{intersect_layers}{osm_difference}"
            f"--min-mask-component-area-ha {min_clean_mask_component_area_ha} "
            f"--clip-region-code {source_region_code} "
            f"{base_options}"
            f"{full_approval_arg}"
            f"{_forest_refine_area_guard_args(region['area_ha'])}"
        )
    return (
        f"{command}{source_region}{source_layers}{intersect_layers}{osm_difference}"
        f"--min-mask-component-area-ha {min_clean_mask_component_area_ha} "
        f"{base_options}"
        f"{full_approval_arg}"
        "--min-area-ha 1 --max-area-ha 30000000"
    )


def _full_approval_arg(args) -> str:
    return f"{STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG} " if getattr(args, "ack_full_turkey_user_approved", False) else ""


def _area_guard_args(area_ha: float) -> str:
    min_area = max(0.01, float(area_ha) * 0.5)
    max_area = max(min_area, float(area_ha) * 1.5)
    return f"--min-area-ha {round(min_area, 2)} --max-area-ha {round(max_area, 2)}"


def _forest_refine_area_guard_args(seed_area_ha: float) -> str:
    min_area = max(0.01, float(seed_area_ha) * 0.05)
    max_area = max(min_area, float(seed_area_ha) * 1.05)
    return f"--min-area-ha {round(min_area, 2)} --max-area-ha {round(max_area, 2)}"


def _format_coord(value: Any) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def _min_clean_mask_component_area_ha(args) -> float:
    return float(
        getattr(args, "min_clean_mask_component_area_ha", DEFAULT_MIN_CLEAN_MASK_COMPONENT_AREA_HA)
    )


def _partitions_required(expected_grid_cells: int) -> bool:
    return expected_grid_cells >= 1_000_000


def _validate_args(args) -> None:
    if args.target_hectares <= 0:
        raise ValueError("target_hectares must be greater than zero")
    if args.grid_size_m <= 0:
        raise ValueError("grid_size_m must be greater than zero")
    if args.grid_tile_size_m <= 0:
        raise ValueError("grid_tile_size_m must be greater than zero")
    if args.ogm_tile_size_deg <= 0:
        raise ValueError("ogm_tile_size_deg must be greater than zero")
    if args.grid_batch_cells <= 0:
        raise ValueError("grid_batch_cells must be greater than zero")
    if args.expected_layer_a_features < 0:
        raise ValueError("expected_layer_a_features must not be negative")
    if args.full_scale_projected_srid <= 0:
        raise ValueError("full_scale_projected_srid must be greater than zero")
    if args.min_clean_mask_component_area_ha < 0:
        raise ValueError("min-clean-mask-component-area-ha must not be negative")


def _plan_projected_srid(args, region: dict[str, Any]) -> int:
    if args.acceptance_scope == "public-ogm":
        return args.full_scale_projected_srid
    if args.target_hectares >= 1_000_000 and (
        args.acceptance_scope != "public-ogm" or not region.get("forest_only")
    ):
        return args.full_scale_projected_srid
    return projected_srid_for_lon_lat(region["centroid_lon"], region["centroid_lat"])


def _selected_projected_srid(args, region: dict[str, Any], current: dict[str, Any]) -> int:
    latest_grid_run = current.get("latest_grid_run") or {}
    projected_srid = latest_grid_run.get("projected_srid")
    if projected_srid:
        return int(projected_srid)
    return _plan_projected_srid(args, region)


def _selected_grid_tile_count(
    args,
    region: dict[str, Any],
    current: dict[str, Any],
    target_grid_cells: int,
) -> int:
    latest_grid_run = current.get("latest_grid_run") or {}
    total_tiles = latest_grid_run.get("total_tiles")
    if total_tiles:
        return int(total_tiles)
    if args.acceptance_scope == "full-scale":
        return estimate_grid_tiles_from_cell_target(
            target_grid_cells,
            args.grid_size_m,
            args.grid_tile_size_m,
        )
    return estimate_grid_tiles(
        region["projected_width_m"],
        region["projected_height_m"],
        args.grid_tile_size_m,
    )


def _grid_projection_strategy(args, current: dict[str, Any]) -> str:
    if current.get("latest_grid_run", {}).get("projected_srid"):
        return "existing_grid_run_projected_srid"
    if args.acceptance_scope == "public-ogm":
        return "equal_area_public_forest"
    if args.target_hectares >= 1_000_000:
        return "equal_area_full_scale"
    return "regional_projected_srid"


def _load_region(db, region_code: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH region AS (
                SELECT
                    id,
                    code,
                    name,
                    country_code,
                    metadata,
                    geom,
                    ST_PointOnSurface(geom) AS center
                FROM regions
                WHERE code = :region_code
            ),
            srid AS (
                SELECT
                    *,
                    CASE
                        WHEN ST_Y(center) >= 0 THEN 32600
                        ELSE 32700
                    END + LEAST(60, GREATEST(1, floor((ST_X(center) + 180) / 6)::integer + 1)) AS projected_srid
                FROM region
            )
            SELECT
                code,
                name,
                country_code,
                COALESCE(metadata->>'forest_only', 'false') = 'true' AS forest_only,
                metadata->>'stage3_region_source' AS region_source,
                COALESCE(metadata->'forest_mask'->>'source_region_code', code) AS source_region_code,
                ST_Area(ST_Transform(geom, projected_srid)) / 10000.0 AS area_ha,
                ST_XMin(geom) AS min_lon,
                ST_YMin(geom) AS min_lat,
                ST_XMax(geom) AS max_lon,
                ST_YMax(geom) AS max_lat,
                ST_X(center) AS centroid_lon,
                ST_Y(center) AS centroid_lat,
                ST_XMax(ST_Envelope(ST_Transform(geom, projected_srid)))
                    - ST_XMin(ST_Envelope(ST_Transform(geom, projected_srid))) AS projected_width_m,
                ST_YMax(ST_Envelope(ST_Transform(geom, projected_srid)))
                    - ST_YMin(ST_Envelope(ST_Transform(geom, projected_srid))) AS projected_height_m
            FROM srid
            """
        ),
        {"region_code": region_code},
    ).mappings().first()
    if row is None:
        raise SystemExit(
            f"region not found: {region_code}. Run run_stage3_prepare_region first."
        )
    return dict(row)


def _load_current_counts(db, params: dict[str, Any], region: dict[str, Any]) -> dict[str, Any]:
    source_region_code = str(region.get("source_region_code") or params["region_code"])
    counts = dict(
        db.execute(
            text(
                """
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
                        WHERE parcel.is_current
                          AND parcel.source_layer = ANY(:layer_a_source_layers)
                          AND parcel.attributes->>'region_code' = :source_region_code
                    ) AS forest_mask_source_features,
                    (
                        SELECT count(*)
                        FROM ogm_parcels parcel
                        WHERE parcel.is_current
                          AND parcel.source_layer = ANY(:ogm_layer_a_source_layers)
                          AND parcel.attributes->>'region_code' = :source_region_code
                    ) AS ogm_layer_a_features
                """
            ),
            {
                **params,
                "source_region_code": source_region_code,
                "ogm_layer_a_source_layers": [SOURCE_LAYER_BOLME, SOURCE_LAYER_MESCERE],
            },
        ).mappings().one()
    )
    counts["source_region_code"] = source_region_code
    counts["layer_a_features"] = int(
        counts["ogm_layer_a_features"] or counts["forest_mask_source_features"] or 0
    )
    counts["grid_runs"] = _count_tracking_runs(
        db,
        "stage3_grid_generation_runs",
        params["region_code"],
        params["grid_version"],
    )
    counts["latest_completed_grid_cells"] = _latest_completed_grid_cells(
        db,
        params["region_code"],
        params["grid_version"],
    )
    counts["latest_grid_run"] = _latest_grid_run(
        db,
        params["region_code"],
        params["grid_version"],
    )
    counts["layer_a_runs"] = _count_tracking_runs(
        db,
        "stage3_layer_a_ingest_runs",
        source_region_code,
        "",
    )
    return counts


def _latest_grid_run(db, region_code: str, grid_version: str) -> dict[str, Any]:
    if not _table_exists(db, "stage3_grid_generation_runs"):
        return {}
    row = db.execute(
        text(
            """
            SELECT
                run.id,
                run.status,
                run.projected_srid,
                run.grid_size_m,
                run.tile_size_m,
                run.total_tiles,
                run.processed_tiles,
                run.inserted_cells
            FROM stage3_grid_generation_runs run
            JOIN regions region ON region.id = run.region_id
            WHERE region.code = :region_code
              AND run.grid_version = :grid_version
            ORDER BY run.started_at DESC NULLS LAST, run.id DESC
            LIMIT 1
            """
        ),
        {"region_code": region_code, "grid_version": grid_version},
    ).mappings().first()
    return dict(row) if row else {}


def _latest_completed_grid_cells(db, region_code: str, grid_version: str) -> int:
    if not _table_exists(db, "stage3_grid_generation_runs"):
        return 0
    return int(
        db.execute(
            text(
                """
                SELECT COALESCE(run.inserted_cells, 0)
                FROM stage3_grid_generation_runs run
                JOIN regions region ON region.id = run.region_id
                WHERE region.code = :region_code
                  AND run.grid_version = :grid_version
                  AND run.status = 'completed'
                ORDER BY run.finished_at DESC NULLS LAST, run.id DESC
                LIMIT 1
                """
            ),
            {"region_code": region_code, "grid_version": grid_version},
        ).scalar()
        or 0
    )


def _load_schema_status(db) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
                to_regclass('public.stage3_grid_generation_runs') IS NOT NULL
                    AND to_regclass('public.stage3_grid_generation_tiles') IS NOT NULL AS grid_generation_tracking,
                to_regclass('public.stage3_layer_a_ingest_runs') IS NOT NULL
                    AND to_regclass('public.stage3_layer_a_ingest_tiles') IS NOT NULL AS layer_a_ingest_tracking,
                EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND indexname IN (
                          'grid_cells_geom_gix',
                          'ogm_parcels_geom_gix',
                          'grid_cells_region_version_uuid_idx',
                          'ogm_parcels_current_region_layer_idx'
                      )
                    GROUP BY schemaname
                    HAVING count(*) = 4
                ) AS core_spatial_indexes,
                EXISTS (
                    SELECT 1
                    FROM pg_partitioned_table partitioned
                    JOIN pg_class cls ON cls.oid = partitioned.partrelid
                    JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
                    WHERE cls.relname IN ('grid_cells', 'sentinel_observations', 'verification_results')
                      AND namespace.nspname = 'public'
                      AND lower(pg_get_partkeydef(cls.oid)) LIKE '%region%'
                    GROUP BY namespace.nspname
                    HAVING count(*) = 3
                ) AS partitioning_detected
            """
        )
    ).mappings().one()
    return {key: bool(value) for key, value in dict(row).items()}


def _count_tracking_runs(db, table_name: str, region_code: str, grid_version: str) -> int:
    if not _table_exists(db, table_name):
        return 0
    if table_name == "stage3_grid_generation_runs":
        return int(
            db.execute(
                text(
                    """
                    SELECT count(*)
                    FROM stage3_grid_generation_runs run
                    JOIN regions region ON region.id = run.region_id
                    WHERE region.code = :region_code
                      AND run.grid_version = :grid_version
                    """
                ),
                {"region_code": region_code, "grid_version": grid_version},
            ).scalar_one()
        )
    return int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM stage3_layer_a_ingest_runs run
                JOIN regions region ON region.id = run.region_id
                WHERE region.code = :region_code
                """
            ),
            {"region_code": region_code},
        ).scalar_one()
    )


def _table_exists(db, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


if __name__ == "__main__":
    main()
