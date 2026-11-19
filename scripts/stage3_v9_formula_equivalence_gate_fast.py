from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

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
    parser = argparse.ArgumentParser(description="Fast V10 formula equivalence gate.")
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
    parser.add_argument("--max-diff-area-m2", type=float, default=1.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    source_layers = _layers(args.source_layers)
    intersect_layers = _layers(args.intersect_source_layers)
    exclusion_layers = _layers(args.exclusion_source_layers)
    all_layers = source_layers + intersect_layers + exclusion_layers

    with SessionLocal() as db:
        db.execute(text("SET LOCAL statement_timeout = '90min'"))
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
            max_diff_area_m2=args.max_diff_area_m2,
            rebuilt=rebuilt,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


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
    max_diff_area_m2: float,
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
                    CAST(:min_component_area_ha AS float) * 10000.0 AS min_component_area_m2
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
                SELECT piece.i, piece.j, ST_UnaryUnion(ST_Collect(piece.geom)) AS geom
                FROM stage3_fragment_pieces piece
                JOIN stage3_fragment_accepted_pieces accepted ON accepted.piece_id = piece.id
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
                SELECT count(*)::bigint AS rebuilt_fragment_count, COALESCE(sum(piece.area_m2), 0)::float AS rebuilt_area_m2
                FROM stage3_fragment_pieces piece
                JOIN stage3_fragment_accepted_pieces accepted ON accepted.piece_id = piece.id
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
            outside_region AS (
                SELECT
                    COALESCE(sum(ST_Area(ST_Difference(component.geom_projected, clip_region.geom))), 0)::float AS outside_region_m2,
                    count(*) FILTER (WHERE ST_Area(ST_Difference(component.geom_projected, clip_region.geom)) > 0)::bigint AS components_outside_region
                FROM components component
                CROSS JOIN clip_region
            ),
            source_live AS (
                SELECT parcel.id, parcel.source_layer
                FROM ogm_parcels parcel, params
                WHERE parcel.is_current
                  AND parcel.attributes->>'region_code' = params.source_region_code
                  AND (
                    parcel.source_layer = ANY(params.source_layers)
                    OR parcel.source_layer = ANY(params.intersect_layers)
                    OR parcel.source_layer = ANY(params.exclusion_layers)
                  )
                  AND parcel.geom IS NOT NULL
                  AND NOT ST_IsEmpty(parcel.geom)
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
                outside_region.outside_region_m2,
                outside_region.components_outside_region,
                source_freshness.live_source_rows,
                source_freshness.live_source_rows_missing_cache,
                stale_cache.stale_cache_rows,
                filtered_source_cache.source_cache_pre_filtered_rows
            FROM component_metrics
            CROSS JOIN rebuilt_metrics
            CROSS JOIN diff_metrics
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
        <= max_diff_area_m2,
        "no_formula_missing_area": float(row["expected_missing_m2"] or 0) <= max_diff_area_m2,
        "no_formula_extra_area": float(row["actual_extra_m2"] or 0) <= max_diff_area_m2,
        "no_component_outside_clip_region": float(row["outside_region_m2"] or 0) <= max_diff_area_m2
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
        "outside_region_m2": round(float(row["outside_region_m2"] or 0), 6),
        "components_outside_region": int(row["components_outside_region"] or 0),
        "live_source_rows": int(row["live_source_rows"] or 0),
        "live_source_rows_missing_cache": int(row["live_source_rows_missing_cache"] or 0),
        "stale_cache_rows": int(row["stale_cache_rows"] or 0),
        "source_cache_pre_filtered_rows": int(row["source_cache_pre_filtered_rows"] or 0),
        "checks": checks,
    }


if __name__ == "__main__":
    main()
