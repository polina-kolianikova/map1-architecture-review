from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS,
    STAGE3_PUBLIC_V9_FORMULA,
    STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS,
    STAGE3_PUBLIC_V9_SOURCE_LAYERS,
    stage3_public_v9_formula_manifest_matches,
    stage3_public_v9_manifest_hash,
)


DEFAULT_PROJECTED_SRID = 6933
DEFAULT_SOURCE_LAYERS = STAGE3_PUBLIC_V9_SOURCE_LAYERS
DEFAULT_INTERSECT_SOURCE_LAYERS = STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS
DEFAULT_EXCLUSION_SOURCE_LAYERS = STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strict geometry integrity check for final Stage 3 clean mask components."
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--source-region-code", required=True)
    parser.add_argument("--clip-region-code", default="")
    parser.add_argument("--mask-version", default="")
    parser.add_argument("--source-layers", default=",".join(DEFAULT_SOURCE_LAYERS))
    parser.add_argument("--intersect-source-layers", default=",".join(DEFAULT_INTERSECT_SOURCE_LAYERS))
    parser.add_argument("--exclusion-source-layers", default=",".join(DEFAULT_EXCLUSION_SOURCE_LAYERS))
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--min-component-area-ha", type=float, default=1.0)
    parser.add_argument("--max-overlap-area-m2", type=float, default=1.0)
    parser.add_argument("--max-outside-source-area-m2", type=float, default=1.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.min_component_area_ha < 0:
        raise ValueError("min-component-area-ha must not be negative")
    if args.max_overlap_area_m2 < 0 or args.max_outside_source_area_m2 < 0:
        raise ValueError("max area tolerances must not be negative")

    source_layers = _parse_layers(args.source_layers)
    intersect_layers = _parse_layers(args.intersect_source_layers)
    exclusion_layers = _parse_layers(args.exclusion_source_layers)
    if not source_layers:
        raise ValueError("at least one source layer is required")
    if not exclusion_layers:
        raise ValueError("at least one exclusion source layer is required")

    with SessionLocal() as db:
        report = clean_mask_integrity_report(
            db,
            region_code=args.region_code,
            source_region_code=args.source_region_code,
            clip_region_code=args.clip_region_code,
            mask_version=args.mask_version or args.region_code,
            source_layers=source_layers,
            intersect_source_layers=intersect_layers,
            exclusion_source_layers=exclusion_layers,
            projected_srid=args.projected_srid,
            min_component_area_ha=args.min_component_area_ha,
            max_overlap_area_m2=args.max_overlap_area_m2,
            max_outside_source_area_m2=args.max_outside_source_area_m2,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


def clean_mask_integrity_report(
    db,
    *,
    region_code: str,
    source_region_code: str,
    clip_region_code: str = "",
    mask_version: str,
    source_layers: list[str],
    intersect_source_layers: list[str],
    exclusion_source_layers: list[str],
    projected_srid: int,
    min_component_area_ha: float = 1.0,
    max_overlap_area_m2: float = 1.0,
    max_outside_source_area_m2: float = 1.0,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH params AS (
                SELECT
                    CAST(:region_code AS text) AS region_code,
                    CAST(:source_region_code AS text) AS source_region_code,
                    NULLIF(CAST(:clip_region_code AS text), '') AS clip_region_code,
                    CAST(:mask_version AS text) AS mask_version,
                    CAST(:source_layers AS text[]) AS source_layers,
                    CAST(:intersect_source_layers AS text[]) AS intersect_source_layers,
                    CAST(:exclusion_source_layers AS text[]) AS exclusion_source_layers,
                    CAST(:projected_srid AS int) AS projected_srid,
                    CAST(:min_component_area_ha AS float) * 10000.0 AS min_component_area_m2,
                    CAST(:max_overlap_area_m2 AS float) AS max_overlap_area_m2,
                    CAST(:max_outside_source_area_m2 AS float) AS max_outside_source_area_m2
            ),
            region_scope AS (
                SELECT region.id AS region_id
                FROM regions region, params
                WHERE region.code = params.region_code
            ),
            analysis_region AS (
                SELECT ST_Transform(region.geom, params.projected_srid) AS geom
                FROM regions region, params
                WHERE region.code = COALESCE(params.clip_region_code, params.source_region_code)
            ),
            components AS (
                SELECT component.*
                FROM stage3_region_mask_components component, region_scope, params
                WHERE component.region_id = region_scope.region_id
                  AND component.mask_version = params.mask_version
                  AND component.projected_srid = params.projected_srid
            ),
            source_cache AS (
                SELECT cache.*
                FROM stage3_source_mask_cache cache, params
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.projected_srid = params.projected_srid
                  AND cache.source_layer = ANY(params.source_layers)
            ),
            intersect_cache AS (
                SELECT cache.*
                FROM stage3_source_mask_cache cache, params
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.projected_srid = params.projected_srid
                  AND cache.source_layer = ANY(params.intersect_source_layers)
            ),
            exclusion_cache AS (
                SELECT cache.*
                FROM stage3_source_mask_cache cache, params
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.projected_srid = params.projected_srid
                  AND cache.source_layer = ANY(params.exclusion_source_layers)
            ),
            empty_geom AS (
                SELECT ST_SetSRID(ST_GeomFromText('POLYGON EMPTY'), params.projected_srid) AS geom
                FROM params
            ),
            component_metrics AS (
                SELECT
                    count(*)::bigint AS component_count,
                    count(*) FILTER (
                        WHERE geom_projected IS NULL OR ST_IsEmpty(geom_projected)
                    )::bigint AS empty_components,
                    count(*) FILTER (
                        WHERE NOT ST_IsValid(geom_projected)
                    )::bigint AS invalid_components,
                    count(*) FILTER (
                        WHERE (
                            CASE
                                WHEN metadata ? 'connected_component_area_ha'
                                THEN NULLIF(metadata->>'connected_component_area_ha', '')::float * 10000.0
                                ELSE ST_Area(geom_projected)
                            END
                        ) < (SELECT min_component_area_m2 FROM params)
                    )::bigint AS tiny_components,
                    COALESCE(sum(ST_Area(geom_projected)), 0)::float AS component_area_m2
                FROM components
            ),
            exclusion_overlap_by_component AS (
                SELECT
                    component.id AS component_id,
                    COALESCE(hit.area_m2, 0)::float AS overlap_area_m2
                FROM components component
                LEFT JOIN LATERAL (
                    SELECT ST_Area(
                        ST_UnaryUnion(
                            ST_Collect(ST_Intersection(component.geom_projected, exclusion.geom_projected))
                        )
                    )::float AS area_m2
                    FROM exclusion_cache exclusion
                    WHERE exclusion.geom_projected && component.geom_projected
                      AND ST_Intersects(exclusion.geom_projected, component.geom_projected)
                ) hit ON TRUE
            ),
            source_outside_by_component AS (
                SELECT
                    component.id AS component_id,
                    COALESCE(outside.area_m2, 0)::float AS outside_source_area_m2
                FROM components component
                LEFT JOIN LATERAL (
                    SELECT ST_Area(
                        CASE
                            WHEN formula_base.geom IS NULL THEN component.geom_projected
                            ELSE ST_Difference(component.geom_projected, formula_base.geom)
                        END
                    )::float AS area_m2
                    FROM params
                    CROSS JOIN LATERAL (
                        SELECT ST_UnaryUnion(ST_Collect(source.geom_projected)) AS geom
                        FROM source_cache source
                        WHERE source.geom_projected && component.geom_projected
                          AND ST_Intersects(source.geom_projected, component.geom_projected)
                    ) source_union
                    CROSS JOIN LATERAL (
                        SELECT ST_UnaryUnion(ST_Collect(intersect_source.geom_projected)) AS geom
                        FROM intersect_cache intersect_source
                        WHERE intersect_source.geom_projected && component.geom_projected
                          AND ST_Intersects(intersect_source.geom_projected, component.geom_projected)
                    ) intersect_union
                    CROSS JOIN LATERAL (
                        SELECT CASE
                            WHEN source_union.geom IS NULL THEN NULL
                            WHEN cardinality(params.intersect_source_layers) = 0 THEN ST_CollectionExtract(
                                ST_MakeValid(source_union.geom),
                                3
                            )
                            WHEN intersect_union.geom IS NULL THEN NULL
                            ELSE ST_CollectionExtract(
                                ST_MakeValid(ST_Intersection(source_union.geom, intersect_union.geom)),
                                3
                            )
                        END AS geom
                    ) formula_base
                ) outside ON TRUE
            ),
            source_residual_pieces AS (
                SELECT
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_Difference(
                                ST_Difference(
                                    COALESCE(formula_base.geom, empty_geom.geom),
                                    COALESCE(exclusion_union.geom, empty_geom.geom)
                                ),
                                COALESCE(component_union.geom, empty_geom.geom)
                            )
                        ),
                        3
                    ) AS geom
                FROM source_cache source
                CROSS JOIN params
                CROSS JOIN empty_geom
                CROSS JOIN analysis_region
                CROSS JOIN LATERAL (
                    SELECT ST_CollectionExtract(
                        ST_MakeValid(ST_Intersection(source.geom_projected, analysis_region.geom)),
                        3
                    ) AS geom
                ) source_base
                LEFT JOIN LATERAL (
                    SELECT ST_UnaryUnion(ST_Collect(intersect_source.geom_projected)) AS geom
                    FROM intersect_cache intersect_source
                    WHERE source_base.geom IS NOT NULL
                      AND intersect_source.geom_projected && source_base.geom
                      AND ST_Intersects(intersect_source.geom_projected, source_base.geom)
                ) intersect_union ON TRUE
                CROSS JOIN LATERAL (
                    SELECT CASE
                        WHEN cardinality(params.intersect_source_layers) = 0 THEN source_base.geom
                        WHEN intersect_union.geom IS NULL THEN NULL
                        ELSE ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(source_base.geom, intersect_union.geom)),
                            3
                        )
                    END AS geom
                ) formula_base
                LEFT JOIN LATERAL (
                    SELECT ST_UnaryUnion(ST_Collect(exclusion.geom_projected)) AS geom
                    FROM exclusion_cache exclusion
                    WHERE formula_base.geom IS NOT NULL
                      AND exclusion.geom_projected && formula_base.geom
                      AND ST_Intersects(exclusion.geom_projected, formula_base.geom)
                ) exclusion_union ON TRUE
                LEFT JOIN LATERAL (
                    SELECT ST_UnaryUnion(ST_Collect(component.geom_projected)) AS geom
                    FROM components component
                    WHERE formula_base.geom IS NOT NULL
                      AND component.geom_projected && formula_base.geom
                      AND ST_Intersects(component.geom_projected, formula_base.geom)
                ) component_union ON TRUE
                WHERE source.geom_projected && analysis_region.geom
                  AND ST_Intersects(source.geom_projected, analysis_region.geom)
                  AND source_base.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source_base.geom)
            ),
            source_residual_union AS (
                SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM source_residual_pieces
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_Area(geom) > 0
            ),
            source_missing_components AS (
                SELECT (ST_Dump(geom)).geom AS geom
                FROM source_residual_union
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            source_missing_metrics AS (
                SELECT
                    count(*)::bigint AS source_missing_component_count,
                    count(*) FILTER (
                        WHERE ST_Area(geom) >= (SELECT min_component_area_m2 FROM params)
                    )::bigint AS source_missing_large_component_count,
                    COALESCE(sum(ST_Area(geom)), 0)::float AS source_missing_area_m2,
                    COALESCE(
                        sum(ST_Area(geom)) FILTER (
                            WHERE ST_Area(geom) >= (SELECT min_component_area_m2 FROM params)
                        ),
                        0
                    )::float AS source_missing_large_area_m2,
                    COALESCE(max(ST_Area(geom)), 0)::float AS max_source_missing_component_m2
                FROM source_missing_components
            ),
            exclusion_overlap_metrics AS (
                SELECT
                    COALESCE(sum(overlap_area_m2), 0)::float AS exclusion_overlap_area_m2,
                    count(*) FILTER (
                        WHERE overlap_area_m2 > (SELECT max_overlap_area_m2 FROM params)
                    )::bigint AS components_with_exclusion_overlap,
                    COALESCE(max(overlap_area_m2), 0)::float AS max_component_exclusion_overlap_m2
                FROM exclusion_overlap_by_component
            ),
            source_outside_metrics AS (
                SELECT
                    COALESCE(sum(outside_source_area_m2), 0)::float AS outside_source_area_m2,
                    count(*) FILTER (
                        WHERE outside_source_area_m2 > (SELECT max_outside_source_area_m2 FROM params)
                    )::bigint AS components_outside_source,
                    COALESCE(max(outside_source_area_m2), 0)::float AS max_component_outside_source_m2
                FROM source_outside_by_component
            ),
            source_region_outside_metrics AS (
                SELECT
                    COALESCE(sum(outside.area_m2), 0)::float AS outside_source_region_area_m2,
                    count(*) FILTER (
                        WHERE outside.area_m2 > (SELECT max_outside_source_area_m2 FROM params)
                    )::bigint AS components_outside_source_region,
                    COALESCE(max(outside.area_m2), 0)::float AS max_component_outside_source_region_m2
                FROM components component
                CROSS JOIN analysis_region
                CROSS JOIN LATERAL (
                    SELECT ST_Area(
                        CASE
                            WHEN analysis_region.geom IS NULL THEN component.geom_projected
                            ELSE ST_Difference(component.geom_projected, analysis_region.geom)
                        END
                    )::float AS area_m2
                ) outside
            ),
            source_live AS (
                SELECT parcel.id, parcel.source_layer
                FROM ogm_parcels parcel, params, analysis_region
                WHERE parcel.is_current
                  AND parcel.attributes->>'region_code' = params.source_region_code
                  AND (
                    parcel.source_layer = ANY(params.source_layers)
                    OR parcel.source_layer = ANY(params.intersect_source_layers)
                    OR parcel.source_layer = ANY(params.exclusion_source_layers)
                  )
                  AND parcel.geom IS NOT NULL
                  AND NOT ST_IsEmpty(parcel.geom)
                  AND ST_Intersects(ST_MakeValid(parcel.geom), ST_Transform(analysis_region.geom, 4326))
                  AND ST_Area(
                      ST_Intersection(
                          ST_Transform(ST_MakeValid(parcel.geom), params.projected_srid),
                          analysis_region.geom
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
                    OR cache.source_layer = ANY(params.intersect_source_layers)
                    OR cache.source_layer = ANY(params.exclusion_source_layers)
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
                    OR cache.source_layer = ANY(params.intersect_source_layers)
                  )
                  AND COALESCE(cache.metadata->'clean_mask_policy', '{}'::jsonb) <> '{}'::jsonb
            )
            SELECT
                component_metrics.*,
                exclusion_overlap_metrics.exclusion_overlap_area_m2,
                exclusion_overlap_metrics.components_with_exclusion_overlap,
                exclusion_overlap_metrics.max_component_exclusion_overlap_m2,
                source_outside_metrics.outside_source_area_m2,
                source_outside_metrics.components_outside_source,
                source_outside_metrics.max_component_outside_source_m2,
                source_region_outside_metrics.outside_source_region_area_m2,
                source_region_outside_metrics.components_outside_source_region,
                source_region_outside_metrics.max_component_outside_source_region_m2,
                source_missing_metrics.source_missing_component_count,
                source_missing_metrics.source_missing_large_component_count,
                source_missing_metrics.source_missing_area_m2,
                source_missing_metrics.source_missing_large_area_m2,
                source_missing_metrics.max_source_missing_component_m2,
                source_freshness.live_source_rows,
                source_freshness.live_source_rows_missing_cache,
                stale_cache.stale_cache_rows,
                filtered_source_cache.source_cache_pre_filtered_rows
            FROM component_metrics
            CROSS JOIN exclusion_overlap_metrics
            CROSS JOIN source_outside_metrics
            CROSS JOIN source_region_outside_metrics
            CROSS JOIN source_missing_metrics
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
            "intersect_source_layers": intersect_source_layers,
            "exclusion_source_layers": exclusion_source_layers,
            "projected_srid": projected_srid,
            "min_component_area_ha": min_component_area_ha,
            "max_overlap_area_m2": max_overlap_area_m2,
            "max_outside_source_area_m2": max_outside_source_area_m2,
        },
    ).mappings().one()
    formula_manifest_matches = stage3_public_v9_formula_manifest_matches(
        source_layers=source_layers,
        intersect_source_layers=intersect_source_layers,
        difference_source_layers=exclusion_source_layers,
        min_mask_component_area_ha=min_component_area_ha,
        projected_srid=projected_srid,
    )
    checks = {
        "client_formula_manifest_matches": formula_manifest_matches,
        "components_present": int(row["component_count"] or 0) > 0,
        "components_valid": int(row["invalid_components"] or 0) == 0,
        "components_nonempty": int(row["empty_components"] or 0) == 0,
        "no_tiny_components": int(row["tiny_components"] or 0) == 0,
        "no_component_exclusion_overlap": float(row["exclusion_overlap_area_m2"] or 0) <= max_overlap_area_m2
        and int(row["components_with_exclusion_overlap"] or 0) == 0,
        "no_component_outside_source": float(row["outside_source_area_m2"] or 0) <= max_outside_source_area_m2
        and int(row["components_outside_source"] or 0) == 0,
        "no_component_outside_source_region": float(row["outside_source_region_area_m2"] or 0)
        <= max_outside_source_area_m2
        and int(row["components_outside_source_region"] or 0) == 0,
        "no_missing_source_components_at_or_above_min_area": int(row["source_missing_large_component_count"] or 0) == 0
        and float(row["source_missing_large_area_m2"] or 0) <= max_outside_source_area_m2,
        "source_cache_complete": int(row["live_source_rows"] or 0) > 0
        and int(row["live_source_rows_missing_cache"] or 0) == 0,
        "source_cache_current": int(row["stale_cache_rows"] or 0) == 0,
        "source_cache_unfiltered": int(row["source_cache_pre_filtered_rows"] or 0) == 0,
    }
    checks["ready"] = all(checks.values())
    return {
        "region_code": region_code,
        "source_region_code": source_region_code,
        "clip_region_code": clip_region_code or source_region_code,
        "mask_version": mask_version,
        "formula": STAGE3_PUBLIC_V9_FORMULA,
        "formula_manifest_hash": stage3_public_v9_manifest_hash() if formula_manifest_matches else "",
        "expected_formula_manifest_hash": stage3_public_v9_manifest_hash(),
        "source_layers": source_layers,
        "intersect_source_layers": intersect_source_layers,
        "exclusion_source_layers": exclusion_source_layers,
        "projected_srid": projected_srid,
        "component_count": int(row["component_count"] or 0),
        "component_area_ha": round(float(row["component_area_m2"] or 0) / 10000.0, 6),
        "tiny_components": int(row["tiny_components"] or 0),
        "exclusion_overlap_area_m2": round(float(row["exclusion_overlap_area_m2"] or 0), 6),
        "components_with_exclusion_overlap": int(row["components_with_exclusion_overlap"] or 0),
        "max_component_exclusion_overlap_m2": round(float(row["max_component_exclusion_overlap_m2"] or 0), 6),
        "outside_source_area_m2": round(float(row["outside_source_area_m2"] or 0), 6),
        "components_outside_source": int(row["components_outside_source"] or 0),
        "max_component_outside_source_m2": round(float(row["max_component_outside_source_m2"] or 0), 6),
        "outside_source_region_area_m2": round(float(row["outside_source_region_area_m2"] or 0), 6),
        "components_outside_source_region": int(row["components_outside_source_region"] or 0),
        "max_component_outside_source_region_m2": round(
            float(row["max_component_outside_source_region_m2"] or 0),
            6,
        ),
        "source_missing_component_count": int(row["source_missing_component_count"] or 0),
        "source_missing_large_component_count": int(row["source_missing_large_component_count"] or 0),
        "source_missing_area_m2": round(float(row["source_missing_area_m2"] or 0), 6),
        "source_missing_large_area_m2": round(float(row["source_missing_large_area_m2"] or 0), 6),
        "max_source_missing_component_m2": round(float(row["max_source_missing_component_m2"] or 0), 6),
        "live_source_rows": int(row["live_source_rows"] or 0),
        "live_source_rows_missing_cache": int(row["live_source_rows_missing_cache"] or 0),
        "stale_cache_rows": int(row["stale_cache_rows"] or 0),
        "source_cache_pre_filtered_rows": int(row["source_cache_pre_filtered_rows"] or 0),
        "checks": checks,
    }


def _parse_layers(value: str) -> list[str]:
    return [layer.strip() for layer in value.split(",") if layer.strip()]


if __name__ == "__main__":
    main()
