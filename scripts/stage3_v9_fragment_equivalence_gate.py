from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.commands.run_stage3_prepare_region_fragments import (
    _build_connected_components,
    _require_source_cache,
    _stage_tiles_and_pieces,
)
from app.db.session import SessionLocal
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS,
    STAGE3_PUBLIC_V9_FORMULA,
    STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS,
    STAGE3_PUBLIC_V9_SOURCE_LAYERS,
    stage3_public_v9_formula_manifest_matches,
    stage3_public_v9_manifest_hash,
)


def _layers(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fast V10 full-mask gate: rebuild the client formula in tiled temp tables, "
            "apply the global >=1ha connected-component rule, and spatially compare it "
            "with stored stage3_region_mask_components."
        )
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--source-region-code", required=True)
    parser.add_argument("--clip-region-code", required=True)
    parser.add_argument("--mask-version", required=True)
    parser.add_argument("--source-layers", default=",".join(STAGE3_PUBLIC_V9_SOURCE_LAYERS))
    parser.add_argument("--intersect-source-layers", default=",".join(STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS))
    parser.add_argument("--exclusion-source-layers", default=",".join(STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS))
    parser.add_argument("--projected-srid", type=int, default=6933)
    parser.add_argument("--tile-size-m", type=int, default=10000)
    parser.add_argument("--snap-grid-m", type=float, default=0.01)
    parser.add_argument("--subdivide-max-vertices", type=int, default=256)
    parser.add_argument("--edge-tolerance-m", type=float, default=0.02)
    parser.add_argument("--min-component-area-ha", type=float, default=1.0)
    parser.add_argument("--max-symdiff-area-m2", type=float, default=1.0)
    parser.add_argument("--max-exclusion-overlap-m2", type=float, default=1.0)
    parser.add_argument("--max-outside-region-m2", type=float, default=1.0)
    parser.add_argument(
        "--check-source-cache-geometry", action="store_true",
        help=("opt in to complete raw/cache geometry acceptance for SOURCE region (not clip); "
              "clip raw in 4326 then project directly, no snap/filter; bounded, never sampled"),
    )
    parser.add_argument("--source-cache-max-symdiff-m2", type=_finite_nonnegative, default=0.0001,
                        help="maximum per-parcel AND per-layer symmetric difference in m2 (default: 0.0001)")
    parser.add_argument("--source-cache-max-parcels", type=_positive_int, default=1000,
                        help="refuse larger raw source scopes before geometry work (default: 1000)")
    parser.add_argument("--source-cache-max-rows", type=_positive_int, default=10000,
                        help="refuse larger cache scopes, including subdivisions (default: 10000)")
    parser.add_argument("--source-cache-timeout-ms", type=_positive_int, default=45000,
                        help="per-statement timeout for opt-in check; 16MB work_mem, 2s lock timeout")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    source_layers = _layers(args.source_layers)
    intersect_layers = _layers(args.intersect_source_layers)
    exclusion_layers = _layers(args.exclusion_source_layers)
    all_layers = source_layers + intersect_layers + exclusion_layers

    with SessionLocal() as db:
        geometry_report = None
        if args.check_source_cache_geometry:
            # One snapshot covers count limits, raw/cache comparison and the formula gate.
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            geometry_report = check_source_cache_geometry(
                db, source_region_code=args.source_region_code, source_layers=all_layers,
                projected_srid=args.projected_srid,
                max_symdiff_m2=args.source_cache_max_symdiff_m2,
                max_parcels=args.source_cache_max_parcels,
                max_cache_rows=args.source_cache_max_rows,
                statement_timeout_ms=args.source_cache_timeout_ms,
            )
            if not geometry_report["ready"]:
                _emit_report({
                    "region_code": args.region_code, "source_region_code": args.source_region_code,
                    "clip_region_code": args.clip_region_code, "mask_version": args.mask_version,
                    "formula_checked": False, "source_cache_geometry": geometry_report,
                    "checks": {"source_cache_geometry_matches": False, "ready": False},
                }, args.output)
        db.execute(text("SET LOCAL statement_timeout = '2h'"))
        _require_source_cache(
            db,
            source_region_code=args.source_region_code,
            source_layers=all_layers,
            projected_srid=args.projected_srid,
        )
        _stage_tiles_and_pieces(
            db,
            source_region_code=args.source_region_code,
            clip_region_code=args.clip_region_code,
            source_layers=source_layers,
            intersect_layers=intersect_layers,
            difference_layers=exclusion_layers,
            projected_srid=args.projected_srid,
            tile_size_m=args.tile_size_m,
            snap_grid_m=args.snap_grid_m,
            subdivide_max_vertices=args.subdivide_max_vertices,
        )
        rebuilt = _build_connected_components(
            db,
            min_mask_component_area_ha=args.min_component_area_ha,
            edge_tolerance_m=args.edge_tolerance_m,
        )
        report = _report(
            db,
            region_code=args.region_code,
            source_region_code=args.source_region_code,
            clip_region_code=args.clip_region_code,
            mask_version=args.mask_version,
            source_layers=source_layers,
            intersect_layers=intersect_layers,
            exclusion_layers=exclusion_layers,
            projected_srid=args.projected_srid,
            min_component_area_ha=args.min_component_area_ha,
            max_symdiff_area_m2=args.max_symdiff_area_m2,
            max_exclusion_overlap_m2=args.max_exclusion_overlap_m2,
            max_outside_region_m2=args.max_outside_region_m2,
            rebuilt=rebuilt,
        )

    if geometry_report is not None:
        report["source_cache_geometry"] = geometry_report
        report["checks"]["source_cache_geometry_matches"] = geometry_report["ready"]
        report["checks"]["ready"] = all(report["checks"].values())
    _emit_report(report, args.output)


def _emit_report(report: dict[str, Any], output: str) -> None:
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


def _finite_nonnegative(value: str | float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("tolerance must be finite and nonnegative") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("tolerance must be finite and nonnegative")
    return number


def _positive_int(value: str | int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from exc
    if str(number) != str(value) or number <= 0:
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return number


def source_cache_geometry_sql():
    """Independent oracle: clip raw in 4326, project once; never rebuild cache pieces.

    Both sides cover the entire source region. Clipping to the QA window would
    hide stale/missing pieces outside it or compare a partial raw parcel to its
    complete cache. Per-layer checks also catch accumulated sub-tolerance errors.
    """
    return text("""
        WITH source_region AS MATERIALIZED (
            SELECT ST_MakeValid(geom) AS geom FROM regions WHERE code = :source_region_code
        ), raw_clipped AS MATERIALIZED (
            SELECT parcel.id AS source_parcel_id, parcel.source_layer,
                ST_CollectionExtract(ST_MakeValid(ST_Intersection(
                    ST_MakeValid(parcel.geom), region.geom)), 3) AS geom
            FROM ogm_parcels parcel CROSS JOIN source_region region
            WHERE parcel.is_current
              AND parcel.attributes->>'region_code' = :source_region_code
              AND parcel.source_layer = ANY(:source_layers)
              AND parcel.geom && region.geom
              AND ST_Intersects(ST_MakeValid(parcel.geom), region.geom)
        ), live AS MATERIALIZED (
            SELECT source_parcel_id, source_layer, ST_Transform(geom, :projected_srid) AS geom
            FROM raw_clipped WHERE NOT ST_IsEmpty(geom) AND ST_Area(geom) > 0
        ), cache_rows AS MATERIALIZED (
            SELECT source_parcel_id, source_layer, geom_projected,
                COALESCE(ST_IsValid(geom_projected) AND NOT ST_IsEmpty(geom_projected)
                    AND ST_SRID(geom_projected) = :projected_srid
                    AND ST_GeometryType(geom_projected) IN ('ST_Polygon', 'ST_MultiPolygon'), false) AS valid
            FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
        ), cached AS MATERIALIZED (
            SELECT source_parcel_id, source_layer,
                ST_UnaryUnion(ST_Collect(geom_projected) FILTER (WHERE valid)) AS geom,
                count(*) FILTER (WHERE NOT valid) AS invalid_cache_rows
            FROM cache_rows GROUP BY source_parcel_id, source_layer
        ), comparisons AS (
            SELECT 'parcel' AS kind, COALESCE(l.source_parcel_id, c.source_parcel_id) AS source_parcel_id,
                COALESCE(l.source_layer, c.source_layer) AS source_layer,
                l.source_parcel_id IS NOT NULL AS raw_present,
                c.source_parcel_id IS NOT NULL AS cache_present,
                COALESCE(c.invalid_cache_rows, 0) AS invalid_cache_rows,
                ST_Area(ST_SymDifference(l.geom, c.geom)) AS symdiff_m2
            FROM live l FULL JOIN cached c USING (source_parcel_id, source_layer)
        ), raw_layers AS (
            SELECT source_layer, ST_UnaryUnion(ST_Collect(geom)) AS geom
            FROM live GROUP BY source_layer
        ), cache_layers AS (
            SELECT source_layer, ST_UnaryUnion(ST_Collect(geom)) AS geom,
                sum(invalid_cache_rows) AS invalid_cache_rows
            FROM cached GROUP BY source_layer
        )
        SELECT * FROM comparisons
        UNION ALL
        SELECT 'layer', NULL::bigint, COALESCE(l.source_layer, c.source_layer),
            l.source_layer IS NOT NULL, c.source_layer IS NOT NULL,
            COALESCE(c.invalid_cache_rows, 0), ST_Area(ST_SymDifference(l.geom, c.geom))
        FROM raw_layers l FULL JOIN cache_layers c USING (source_layer)
        ORDER BY kind, source_layer, source_parcel_id
    """)


def _geometry_comparison_passes(row: dict[str, Any], tolerance: float) -> bool:
    value = row["symdiff_m2"]
    return bool(
        row["raw_present"] and row["cache_present"] and not row["invalid_cache_rows"]
        and value is not None and math.isfinite(float(value)) and 0 <= float(value) <= tolerance
    )


def check_source_cache_geometry(
    db, *, source_region_code: str, source_layers: list[str], projected_srid: int,
    max_symdiff_m2: float = 0.0001, max_parcels: int = 1000,
    max_cache_rows: int = 10000, statement_timeout_ms: int = 45000,
) -> dict[str, Any]:
    """Opt-in bounded acceptance, SELECT-only; requires caller's stable snapshot.

    Caps include ALL scope rows before spatial work, not just parcels in a QA
    clip. No national scope is silently sampled. Three preflight statements (two
    capped counts) and one geometry statement each have a timeout; work_mem is per
    operation, not a total memory/disk quota. SAVEPOINT rollback restores settings on
    both success and failure. Database/geometry errors are failed acceptance.
    """
    tolerance = _finite_nonnegative(max_symdiff_m2)
    max_parcels = _positive_int(max_parcels)
    max_cache_rows = _positive_int(max_cache_rows)
    statement_timeout_ms = _positive_int(statement_timeout_ms)
    projected_srid = _positive_int(projected_srid)
    layers = sorted(set(source_layers))
    if not source_region_code.strip() or not layers or any(not layer.strip() for layer in layers):
        raise ValueError("source region and source layers must be nonempty")
    params = {"source_region_code": source_region_code, "source_layers": layers,
              "projected_srid": projected_srid, "parcel_probe_limit": max_parcels + 1,
              "cache_probe_limit": max_cache_rows + 1}
    report = {
        "enabled": True, "scope": "entire_source_region", "source_region_code": source_region_code,
        "source_layers": layers, "projected_srid": projected_srid,
        "transformation": "makevalid_raw_clip_4326_then_direct_projection_no_snap_no_area_filter",
        "max_symdiff_m2": tolerance, "max_parcels": max_parcels,
        "max_cache_rows": max_cache_rows, "statement_timeout_ms": statement_timeout_ms,
        "ready": False,
    }
    try:
        with db.begin_nested() as savepoint:
            db.execute(text("SELECT set_config('statement_timeout', :timeout, true), "
                            "set_config('lock_timeout', '2000', true), "
                            "set_config('work_mem', '16MB', true)"),
                       {"timeout": str(statement_timeout_ms)})
            region_ok = db.execute(text("""
                SELECT count(*) = 1 AND COALESCE(bool_and(geom IS NOT NULL
                    AND ST_SRID(geom) = 4326 AND NOT ST_IsEmpty(geom)), false)
                FROM regions WHERE code = :source_region_code
            """), params).scalar_one()
            raw_count = db.execute(text("""
                SELECT count(*) FROM (
                    SELECT 1 FROM ogm_parcels WHERE is_current
                      AND attributes->>'region_code' = :source_region_code
                      AND source_layer = ANY(:source_layers) LIMIT :parcel_probe_limit
                ) bounded_raw
            """), params).scalar_one()
            cache_count = db.execute(text("""
                SELECT count(*) FROM (
                    SELECT 1 FROM stage3_source_mask_cache
                    WHERE source_region_code = :source_region_code
                      AND projected_srid = :projected_srid AND source_layer = ANY(:source_layers)
                    LIMIT :cache_probe_limit
                ) bounded_cache
            """), params).scalar_one()
            report.update(source_region_valid=bool(region_ok), raw_rows_probed=int(raw_count),
                          cache_rows_probed=int(cache_count))
            if not region_ok or not raw_count or not cache_count:
                report["failure"] = "missing_or_empty_source_region_or_scope"
            elif raw_count > max_parcels or cache_count > max_cache_rows:
                report["failure"] = "scope_limit_exceeded_not_sampled"
            else:
                rows = [dict(row) for row in db.execute(source_cache_geometry_sql(), params).mappings()]
                failed = [row for row in rows if not _geometry_comparison_passes(row, tolerance)]
                layer_rows = [row for row in rows if row["kind"] == "layer"]
                values = [float(row["symdiff_m2"]) for row in rows
                          if row["symdiff_m2"] is not None and math.isfinite(float(row["symdiff_m2"]))]
                # JSON must not serialize NaN/Infinity; still retain the failed comparison.
                for row in rows:
                    if row["symdiff_m2"] is not None:
                        value = float(row["symdiff_m2"])
                        row["symdiff_m2"] = value if math.isfinite(value) else None
                    row["invalid_cache_rows"] = int(row["invalid_cache_rows"])
                report.update(
                    compared_parcels=sum(row["kind"] == "parcel" for row in rows),
                    failed_comparisons=len(failed), failure_examples=failed[:20],
                    layer_comparisons=layer_rows, max_observed_symdiff_m2=max(values, default=None),
                    ready=bool(rows) and not failed
                        and {row["source_layer"] for row in layer_rows} == set(layers),
                )
                if not report["ready"]:
                    report["failure"] = "raw_cache_geometry_or_membership_mismatch"
            savepoint.rollback()  # SELECT-only; restore local timeout/work_mem settings.
    except SQLAlchemyError as exc:
        report.update(ready=False, failure="source_cache_query_failed", error_type=type(exc).__name__)
    return report


def _report(
    db,
    *,
    region_code: str,
    source_region_code: str,
    clip_region_code: str,
    mask_version: str,
    source_layers: list[str],
    intersect_layers: list[str],
    exclusion_layers: list[str],
    projected_srid: int,
    min_component_area_ha: float,
    max_symdiff_area_m2: float,
    max_exclusion_overlap_m2: float,
    max_outside_region_m2: float,
    rebuilt: dict[str, Any],
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH params AS (
                SELECT
                    CAST(:region_code AS text) AS region_code,
                    CAST(:source_region_code AS text) AS source_region_code,
                    CAST(:clip_region_code AS text) AS clip_region_code,
                    CAST(:mask_version AS text) AS mask_version,
                    CAST(:source_layers AS text[]) AS source_layers,
                    CAST(:intersect_layers AS text[]) AS intersect_layers,
                    CAST(:exclusion_layers AS text[]) AS exclusion_layers,
                    CAST(:projected_srid AS int) AS projected_srid,
                    CAST(:min_component_area_ha AS float) * 10000.0 AS min_component_area_m2,
                    CAST(:max_exclusion_overlap_m2 AS float) AS max_exclusion_overlap_m2,
                    CAST(:max_outside_region_m2 AS float) AS max_outside_region_m2
            ),
            region_scope AS (
                SELECT id AS region_id
                FROM regions, params
                WHERE code = params.region_code
            ),
            clip_region AS (
                SELECT ST_Transform(ST_MakeValid(geom), params.projected_srid) AS geom
                FROM regions, params
                WHERE code = params.clip_region_code
            ),
            components AS (
                SELECT component.*
                FROM stage3_region_mask_components component, region_scope, params
                WHERE component.region_id = region_scope.region_id
                  AND component.mask_version = params.mask_version
                  AND component.projected_srid = params.projected_srid
            ),
            expected_tiles AS (
                SELECT
                    piece.i,
                    piece.j,
                    ST_UnaryUnion(ST_Collect(piece.geom)) AS geom
                FROM stage3_fragment_pieces piece
                JOIN stage3_fragment_accepted_pieces accepted
                  ON accepted.piece_id = piece.id
                GROUP BY piece.i, piece.j
            ),
            actual_tiles AS (
                SELECT
                    tile.i,
                    tile.j,
                    ST_UnaryUnion(
                        ST_Collect(
                            ST_CollectionExtract(
                                ST_MakeValid(ST_Intersection(component.geom_projected, tile.geom)),
                                3
                            )
                        )
                    ) AS geom
                FROM stage3_fragment_tiles tile
                JOIN components component
                  ON component.geom_projected && tile.geom
                 AND ST_Intersects(component.geom_projected, tile.geom)
                GROUP BY tile.i, tile.j
            ),
            all_tiles AS (
                SELECT i, j FROM expected_tiles
                UNION
                SELECT i, j FROM actual_tiles
            ),
            empty_geom AS (
                SELECT ST_SetSRID(ST_GeomFromText('POLYGON EMPTY'), params.projected_srid) AS geom
                FROM params
            ),
            tile_diff AS (
                SELECT
                    all_tiles.i,
                    all_tiles.j,
                    ST_Area(
                        ST_Difference(
                            COALESCE(expected_tiles.geom, empty_geom.geom),
                            COALESCE(actual_tiles.geom, empty_geom.geom)
                        )
                    )::float AS expected_missing_m2,
                    ST_Area(
                        ST_Difference(
                            COALESCE(actual_tiles.geom, empty_geom.geom),
                            COALESCE(expected_tiles.geom, empty_geom.geom)
                        )
                    )::float AS actual_extra_m2
                FROM all_tiles
                CROSS JOIN empty_geom
                LEFT JOIN expected_tiles USING (i, j)
                LEFT JOIN actual_tiles USING (i, j)
            ),
            component_metrics AS (
                SELECT
                    count(*)::bigint AS component_fragment_count,
                    count(*) FILTER (WHERE geom_projected IS NULL OR ST_IsEmpty(geom_projected))::bigint AS empty_components,
                    count(*) FILTER (WHERE NOT ST_IsValid(geom_projected))::bigint AS invalid_components,
                    count(*) FILTER (
                        WHERE (
                            CASE
                                WHEN metadata ? 'connected_component_area_ha'
                                THEN NULLIF(metadata->>'connected_component_area_ha', '')::float * 10000.0
                                ELSE ST_Area(geom_projected)
                            END
                        ) < (SELECT min_component_area_m2 FROM params)
                    )::bigint AS tiny_connected_components,
                    COALESCE(sum(ST_Area(geom_projected)), 0)::float AS component_area_m2
                FROM components
            ),
            rebuilt_metrics AS (
                SELECT
                    count(*)::bigint AS rebuilt_fragment_count,
                    COALESCE(sum(piece.area_m2), 0)::float AS rebuilt_area_m2
                FROM stage3_fragment_pieces piece
                JOIN stage3_fragment_accepted_pieces accepted
                  ON accepted.piece_id = piece.id
            ),
            diff_metrics AS (
                SELECT
                    COALESCE(sum(expected_missing_m2), 0)::float AS expected_missing_m2,
                    COALESCE(sum(actual_extra_m2), 0)::float AS actual_extra_m2,
                    COALESCE(max(expected_missing_m2), 0)::float AS max_tile_expected_missing_m2,
                    COALESCE(max(actual_extra_m2), 0)::float AS max_tile_actual_extra_m2,
                    count(*) FILTER (WHERE expected_missing_m2 > 0 OR actual_extra_m2 > 0)::bigint AS diff_tiles
                FROM tile_diff
            ),
            exclusion_overlap AS (
                SELECT
                    COALESCE(sum(hit.area_m2), 0)::float AS exclusion_overlap_m2,
                    COALESCE(max(hit.area_m2), 0)::float AS max_component_exclusion_overlap_m2,
                    count(*) FILTER (WHERE hit.area_m2 > params.max_exclusion_overlap_m2)::bigint
                        AS components_with_exclusion_overlap
                FROM components component
                CROSS JOIN params
                LEFT JOIN LATERAL (
                    SELECT COALESCE(
                        ST_Area(ST_UnaryUnion(ST_Collect(ST_Intersection(component.geom_projected, exclusion.geom_projected)))),
                        0
                    )::float AS area_m2
                    FROM stage3_source_mask_cache exclusion, params
                    WHERE exclusion.source_region_code = params.source_region_code
                      AND exclusion.projected_srid = params.projected_srid
                      AND exclusion.source_layer = ANY(params.exclusion_layers)
                      AND exclusion.geom_projected && component.geom_projected
                      AND ST_Intersects(exclusion.geom_projected, component.geom_projected)
                ) hit ON TRUE
            ),
            outside_region AS (
                SELECT
                    COALESCE(sum(ST_Area(ST_Difference(component.geom_projected, clip_region.geom))), 0)::float AS outside_region_m2,
                    COALESCE(max(ST_Area(ST_Difference(component.geom_projected, clip_region.geom))), 0)::float AS max_component_outside_region_m2,
                    count(*) FILTER (
                        WHERE ST_Area(ST_Difference(component.geom_projected, clip_region.geom)) > params.max_outside_region_m2
                    )::bigint AS components_outside_region
                FROM components component
                CROSS JOIN clip_region
                CROSS JOIN params
            ),
            source_live AS (
                SELECT parcel.id, parcel.source_layer
                FROM ogm_parcels parcel, params, clip_region
                WHERE parcel.is_current
                  AND parcel.attributes->>'region_code' = params.source_region_code
                  AND (
                    parcel.source_layer = ANY(params.source_layers)
                    OR parcel.source_layer = ANY(params.intersect_layers)
                    OR parcel.source_layer = ANY(params.exclusion_layers)
                  )
                  AND parcel.geom IS NOT NULL
                  AND NOT ST_IsEmpty(parcel.geom)
                  AND ST_Intersects(ST_MakeValid(parcel.geom), ST_Transform(clip_region.geom, 4326))
                  AND ST_Area(
                    ST_Intersection(
                      ST_Transform(ST_MakeValid(parcel.geom), params.projected_srid),
                      clip_region.geom
                    )
                  ) > 0
            ),
            source_freshness AS (
                SELECT
                    count(*)::bigint AS live_source_rows,
                    count(*) FILTER (
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM stage3_source_mask_cache cache, params
                            WHERE cache.source_parcel_id = source_live.id
                              AND cache.source_region_code = params.source_region_code
                              AND cache.projected_srid = params.projected_srid
                        )
                    )::bigint AS live_source_rows_missing_cache
                FROM source_live
            ),
            stale_cache AS (
                SELECT count(*)::bigint AS stale_cache_rows
                FROM stage3_source_mask_cache cache
                CROSS JOIN params
                LEFT JOIN ogm_parcels parcel ON parcel.id = cache.source_parcel_id
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.projected_srid = params.projected_srid
                  AND (
                    cache.source_layer = ANY(params.source_layers)
                    OR cache.source_layer = ANY(params.intersect_layers)
                    OR cache.source_layer = ANY(params.exclusion_layers)
                  )
                  AND (
                    parcel.id IS NULL
                    OR NOT parcel.is_current
                    OR parcel.source_layer <> cache.source_layer
                    OR parcel.attributes->>'region_code' <> params.source_region_code
                  )
            ),
            filtered_source_cache AS (
                SELECT count(*)::bigint AS source_cache_pre_filtered_rows
                FROM stage3_source_mask_cache cache
                CROSS JOIN params
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.projected_srid = params.projected_srid
                  AND (
                    cache.source_layer = ANY(params.source_layers)
                    OR cache.source_layer = ANY(params.intersect_layers)
                  )
                  AND COALESCE(cache.metadata->'clean_mask_policy', '{}'::jsonb) <> '{}'::jsonb
            )
            SELECT
                component_metrics.*,
                rebuilt_metrics.rebuilt_fragment_count,
                rebuilt_metrics.rebuilt_area_m2,
                diff_metrics.expected_missing_m2,
                diff_metrics.actual_extra_m2,
                diff_metrics.max_tile_expected_missing_m2,
                diff_metrics.max_tile_actual_extra_m2,
                diff_metrics.diff_tiles,
                exclusion_overlap.exclusion_overlap_m2,
                exclusion_overlap.max_component_exclusion_overlap_m2,
                exclusion_overlap.components_with_exclusion_overlap,
                outside_region.outside_region_m2,
                outside_region.max_component_outside_region_m2,
                outside_region.components_outside_region,
                source_freshness.live_source_rows,
                source_freshness.live_source_rows_missing_cache,
                stale_cache.stale_cache_rows,
                filtered_source_cache.source_cache_pre_filtered_rows
            FROM component_metrics
            CROSS JOIN rebuilt_metrics
            CROSS JOIN diff_metrics
            CROSS JOIN exclusion_overlap
            CROSS JOIN outside_region
            CROSS JOIN source_freshness
            CROSS JOIN stale_cache
            CROSS JOIN filtered_source_cache
            """
        ),
        {
            "region_code": region_code,
            "source_region_code": source_region_code,
            "clip_region_code": clip_region_code,
            "mask_version": mask_version,
            "source_layers": source_layers,
            "intersect_layers": intersect_layers,
            "exclusion_layers": exclusion_layers,
            "projected_srid": projected_srid,
            "min_component_area_ha": min_component_area_ha,
            "max_exclusion_overlap_m2": max_exclusion_overlap_m2,
            "max_outside_region_m2": max_outside_region_m2,
        },
    ).mappings().one()

    formula_manifest_matches = stage3_public_v9_formula_manifest_matches(
        source_layers=source_layers,
        intersect_source_layers=intersect_layers,
        difference_source_layers=exclusion_layers,
        min_mask_component_area_ha=min_component_area_ha,
        projected_srid=projected_srid,
    )
    checks = {
        "client_formula_manifest_matches": formula_manifest_matches,
        "components_present": int(row["component_fragment_count"] or 0) > 0,
        "components_valid": int(row["invalid_components"] or 0) == 0,
        "components_nonempty": int(row["empty_components"] or 0) == 0,
        "no_tiny_connected_components": int(row["tiny_connected_components"] or 0) == 0,
        "rebuilt_formula_fragment_count_matches": int(row["rebuilt_fragment_count"] or 0)
        == int(row["component_fragment_count"] or 0),
        "rebuilt_formula_area_matches": abs(
            float(row["rebuilt_area_m2"] or 0) - float(row["component_area_m2"] or 0)
        )
        <= max_symdiff_area_m2,
        "no_formula_missing_area": float(row["expected_missing_m2"] or 0) <= max_symdiff_area_m2,
        "no_formula_extra_area": float(row["actual_extra_m2"] or 0) <= max_symdiff_area_m2,
        "no_component_exclusion_overlap": float(row["exclusion_overlap_m2"] or 0) <= max_exclusion_overlap_m2
        and int(row["components_with_exclusion_overlap"] or 0) == 0,
        "no_component_outside_clip_region": float(row["outside_region_m2"] or 0) <= max_outside_region_m2
        and int(row["components_outside_region"] or 0) == 0,
        "source_cache_complete": int(row["live_source_rows"] or 0) > 0
        and int(row["live_source_rows_missing_cache"] or 0) == 0,
        "source_cache_current": int(row["stale_cache_rows"] or 0) == 0,
        "source_cache_unfiltered": int(row["source_cache_pre_filtered_rows"] or 0) == 0,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "source_region_code": source_region_code,
        "clip_region_code": clip_region_code,
        "mask_version": mask_version,
        "formula": STAGE3_PUBLIC_V9_FORMULA,
        "formula_manifest_hash": stage3_public_v9_manifest_hash() if formula_manifest_matches else "",
        "expected_formula_manifest_hash": stage3_public_v9_manifest_hash(),
        "source_layers": source_layers,
        "intersect_source_layers": intersect_layers,
        "exclusion_source_layers": exclusion_layers,
        "rebuilt": rebuilt,
        "component_fragment_count": int(row["component_fragment_count"] or 0),
        "component_area_ha": round(float(row["component_area_m2"] or 0) / 10000.0, 6),
        "rebuilt_fragment_count": int(row["rebuilt_fragment_count"] or 0),
        "rebuilt_area_ha": round(float(row["rebuilt_area_m2"] or 0) / 10000.0, 6),
        "expected_missing_m2": round(float(row["expected_missing_m2"] or 0), 6),
        "actual_extra_m2": round(float(row["actual_extra_m2"] or 0), 6),
        "max_tile_expected_missing_m2": round(float(row["max_tile_expected_missing_m2"] or 0), 6),
        "max_tile_actual_extra_m2": round(float(row["max_tile_actual_extra_m2"] or 0), 6),
        "diff_tiles": int(row["diff_tiles"] or 0),
        "exclusion_overlap_m2": round(float(row["exclusion_overlap_m2"] or 0), 6),
        "max_component_exclusion_overlap_m2": round(float(row["max_component_exclusion_overlap_m2"] or 0), 6),
        "components_with_exclusion_overlap": int(row["components_with_exclusion_overlap"] or 0),
        "outside_region_m2": round(float(row["outside_region_m2"] or 0), 6),
        "max_component_outside_region_m2": round(float(row["max_component_outside_region_m2"] or 0), 6),
        "components_outside_region": int(row["components_outside_region"] or 0),
        "live_source_rows": int(row["live_source_rows"] or 0),
        "live_source_rows_missing_cache": int(row["live_source_rows_missing_cache"] or 0),
        "stale_cache_rows": int(row["stale_cache_rows"] or 0),
        "source_cache_pre_filtered_rows": int(row["source_cache_pre_filtered_rows"] or 0),
        "checks": checks,
    }


if __name__ == "__main__":
    main()
