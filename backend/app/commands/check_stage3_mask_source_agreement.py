from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal


DEFAULT_FOREST_SOURCE_LAYER = "osm_forest_mask"
DEFAULT_EXCLUSION_SOURCE_LAYERS = ("osm_nonforest_exclusion",)
DEFAULT_PROJECTED_SRID = 6933


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Check that the final Stage 3 clean mask follows its source forest "
            "and exclusion layers without silent, unexplained losses."
        )
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--source-region-code", required=True)
    parser.add_argument("--mask-version", default="")
    parser.add_argument("--forest-source-layer", default=DEFAULT_FOREST_SOURCE_LAYER)
    parser.add_argument(
        "--exclusion-source-layers",
        default=",".join(DEFAULT_EXCLUSION_SOURCE_LAYERS),
    )
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--min-disagreement-area-ha", type=float, default=1.0)
    parser.add_argument("--max-unexplained-ratio", type=float, default=0.005)
    parser.add_argument("--max-unexplained-component-ha", type=float, default=10.0)
    parser.add_argument("--component-limit", type=int, default=0)
    parser.add_argument("--component-offset", type=int, default=0)
    parser.add_argument("--bbox", default="", help="optional lon_min,lat_min,lon_max,lat_max QA window")
    parser.add_argument("--fail-on-source-disagreement", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.min_disagreement_area_ha < 0:
        raise ValueError("min-disagreement-area-ha must not be negative")
    if args.max_unexplained_ratio < 0:
        raise ValueError("max-unexplained-ratio must not be negative")
    if args.max_unexplained_component_ha < 0:
        raise ValueError("max-unexplained-component-ha must not be negative")
    if args.component_limit < 0 or args.component_offset < 0:
        raise ValueError("component-limit and component-offset must not be negative")

    exclusion_layers = [layer.strip() for layer in args.exclusion_source_layers.split(",") if layer.strip()]
    if not exclusion_layers:
        raise ValueError("at least one exclusion source layer is required")

    bbox = _parse_bbox(args.bbox)
    with SessionLocal() as db:
        report = mask_source_agreement_report(
            db,
            region_code=args.region_code,
            source_region_code=args.source_region_code,
            mask_version=args.mask_version or args.region_code,
            forest_source_layer=args.forest_source_layer,
            exclusion_source_layers=exclusion_layers,
            projected_srid=args.projected_srid,
            min_disagreement_area_ha=args.min_disagreement_area_ha,
            max_unexplained_ratio=args.max_unexplained_ratio,
            max_unexplained_component_ha=args.max_unexplained_component_ha,
            component_limit=args.component_limit,
            component_offset=args.component_offset,
            bbox=bbox,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))

    if args.fail_on_source_disagreement and not report["checks"]["ready"]:
        raise SystemExit(1)


def mask_source_agreement_report(
    db,
    *,
    region_code: str,
    source_region_code: str,
    mask_version: str,
    forest_source_layer: str,
    exclusion_source_layers: list[str],
    projected_srid: int,
    min_disagreement_area_ha: float = 1.0,
    max_unexplained_ratio: float = 0.005,
    max_unexplained_component_ha: float = 10.0,
    component_limit: int = 0,
    component_offset: int = 0,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH params AS (
                SELECT
                    CAST(:region_code AS text) AS region_code,
                    CAST(:source_region_code AS text) AS source_region_code,
                    CAST(:mask_version AS text) AS mask_version,
                    CAST(:forest_source_layer AS text) AS forest_source_layer,
                    CAST(:exclusion_source_layers AS text[]) AS exclusion_source_layers,
                    CAST(:projected_srid AS int) AS projected_srid,
                    (CAST(:min_disagreement_area_ha AS float) * 10000.0) AS min_disagreement_area_m2,
                    (CAST(:max_unexplained_component_ha AS float) * 10000.0) AS material_component_area_m2,
                    CASE
                        WHEN :use_bbox
                        THEN ST_Transform(
                            ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326),
                            :projected_srid
                        )
                        ELSE NULL
                    END AS bbox_geom
            ),
            region_scope AS (
                SELECT region.id AS region_id
                FROM regions region, params
                WHERE region.code = params.region_code
            ),
            forest_source_rows AS (
                SELECT cache.*
                FROM stage3_source_mask_cache cache, params
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.source_layer = params.forest_source_layer
                  AND cache.projected_srid = params.projected_srid
                  AND (
                    params.bbox_geom IS NULL
                    OR (
                        cache.geom_projected && params.bbox_geom
                        AND ST_Intersects(cache.geom_projected, params.bbox_geom)
                    )
                  )
                ORDER BY ST_XMin(cache.geom_projected), ST_YMin(cache.geom_projected), cache.id
                OFFSET :component_offset
                LIMIT NULLIF(:component_limit, 0)
            ),
            forest_source AS (
                SELECT
                    cache.id,
                    ST_MakeValid(
                        CASE
                            WHEN params.bbox_geom IS NULL THEN cache.geom_projected
                            ELSE ST_Intersection(cache.geom_projected, params.bbox_geom)
                        END
                    ) AS geom,
                    ST_Area(
                        ST_MakeValid(
                            CASE
                                WHEN params.bbox_geom IS NULL THEN cache.geom_projected
                                ELSE ST_Intersection(cache.geom_projected, params.bbox_geom)
                            END
                        )
                    )::float AS area_m2
                FROM forest_source_rows cache, params
            ),
            forest_extent AS (
                SELECT ST_Envelope(ST_Collect(forest_source.geom)) AS geom
                FROM forest_source
            ),
            forest_union AS (
                SELECT ST_CollectionExtract(
                    ST_MakeValid(
                        ST_UnaryUnion(
                            ST_Collect(
                                ST_CollectionExtract(
                                    ST_MakeValid(ST_SnapToGrid(forest_source.geom, 0.001)),
                                    3
                                )
                            )
                        )
                    ),
                    3
                ) AS geom
                FROM forest_source
            ),
            final_mask AS (
                SELECT
                    component.id,
                    ST_MakeValid(
                        CASE
                            WHEN params.bbox_geom IS NULL THEN component.geom_projected
                            ELSE ST_Intersection(component.geom_projected, params.bbox_geom)
                        END
                    ) AS geom,
                    ST_Area(
                        ST_MakeValid(
                            CASE
                                WHEN params.bbox_geom IS NULL THEN component.geom_projected
                                ELSE ST_Intersection(component.geom_projected, params.bbox_geom)
                            END
                        )
                    )::float AS area_m2
                FROM stage3_region_mask_components component, params, region_scope, forest_extent
                WHERE component.region_id = region_scope.region_id
                  AND component.mask_version = params.mask_version
                  AND component.projected_srid = params.projected_srid
                  AND forest_extent.geom IS NOT NULL
                  AND component.geom_projected && forest_extent.geom
                  AND (
                    params.bbox_geom IS NULL
                    OR (
                        component.geom_projected && params.bbox_geom
                        AND ST_Intersects(component.geom_projected, params.bbox_geom)
                    )
                  )
            ),
            exclusions AS (
                SELECT
                    cache.id,
                    cache.source_layer,
                    ST_MakeValid(
                        CASE
                            WHEN params.bbox_geom IS NULL THEN cache.geom_projected
                            ELSE ST_Intersection(cache.geom_projected, params.bbox_geom)
                        END
                    ) AS geom,
                    ST_Area(
                        ST_MakeValid(
                            CASE
                                WHEN params.bbox_geom IS NULL THEN cache.geom_projected
                                ELSE ST_Intersection(cache.geom_projected, params.bbox_geom)
                            END
                        )
                    )::float AS area_m2
                FROM stage3_source_mask_cache cache, params, forest_extent
                WHERE cache.source_region_code = params.source_region_code
                  AND cache.source_layer = ANY(params.exclusion_source_layers)
                  AND cache.projected_srid = params.projected_srid
                  AND forest_extent.geom IS NOT NULL
                  AND cache.geom_projected && forest_extent.geom
                  AND (
                    params.bbox_geom IS NULL
                    OR (
                        cache.geom_projected && params.bbox_geom
                        AND ST_Intersects(cache.geom_projected, params.bbox_geom)
                    )
                  )
            ),
            covered_global AS (
                SELECT ST_CollectionExtract(
                    ST_MakeValid(ST_UnaryUnion(ST_Collect(part.geom))),
                    3
                ) AS geom
                FROM (
                    SELECT ST_Intersection(final_mask.geom, forest_union.geom) AS geom
                    FROM final_mask, forest_union
                    WHERE forest_union.geom IS NOT NULL
                      AND final_mask.geom && forest_union.geom
                      AND ST_Intersects(final_mask.geom, forest_union.geom)
                    UNION ALL
                    SELECT ST_Intersection(exclusions.geom, forest_union.geom) AS geom
                    FROM exclusions, forest_union
                    WHERE forest_union.geom IS NOT NULL
                      AND exclusions.geom && forest_union.geom
                      AND ST_Intersects(exclusions.geom, forest_union.geom)
                ) part
                WHERE NOT ST_IsEmpty(part.geom)
            ),
            global_loss_parts AS (
                SELECT ST_Area(loss.geom)::float AS area_m2
                FROM forest_union, covered_global
                CROSS JOIN LATERAL (
                    SELECT (ST_Dump(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                CASE
                                    WHEN forest_union.geom IS NULL THEN NULL
                                    WHEN covered_global.geom IS NULL THEN forest_union.geom
                                    ELSE ST_Difference(
                                        ST_CollectionExtract(
                                            ST_MakeValid(ST_SnapToGrid(forest_union.geom, 0.001)),
                                            3
                                        ),
                                        ST_CollectionExtract(
                                            ST_MakeValid(ST_SnapToGrid(covered_global.geom, 0.001)),
                                            3
                                        )
                                    )
                                END
                            ),
                            3
                        )
                    )).geom AS geom
                ) loss
                WHERE loss.geom IS NOT NULL
                  AND NOT ST_IsEmpty(loss.geom)
            ),
            global_loss_metrics AS (
                SELECT
                    COALESCE(sum(area_m2) FILTER (
                        WHERE area_m2 >= (SELECT min_disagreement_area_m2 FROM params)
                    ), 0)::float AS global_unexplained_removed_area_m2,
                    count(*) FILTER (
                        WHERE area_m2 >= (SELECT min_disagreement_area_m2 FROM params)
                    )::bigint AS global_unexplained_removed_components,
                    COALESCE(max(area_m2), 0)::float AS global_max_removed_m2,
                    COALESCE(max(area_m2) FILTER (
                        WHERE area_m2 >= (SELECT min_disagreement_area_m2 FROM params)
                    ), 0)::float AS global_max_unexplained_removed_m2,
                    COALESCE(sum(area_m2) FILTER (
                        WHERE area_m2 < (SELECT min_disagreement_area_m2 FROM params)
                    ), 0)::float AS global_fragment_removed_area_m2,
                    count(*) FILTER (
                        WHERE area_m2 < (SELECT min_disagreement_area_m2 FROM params)
                    )::bigint AS global_fragment_removed_components
                FROM global_loss_parts
            ),
            forest_component_metrics AS (
                SELECT
                    forest_source.id,
                    forest_source.area_m2,
                    COALESCE(retained.retained_m2, 0)::float AS retained_m2,
                    COALESCE(excluded.excluded_m2, 0)::float AS excluded_m2,
                    COALESCE(material_loss.material_loss_m2, 0)::float AS material_loss_m2,
                    COALESCE(material_loss.material_loss_components, 0)::bigint AS material_loss_components,
                    COALESCE(material_loss.max_material_loss_m2, 0)::float AS max_material_loss_m2,
                    COALESCE(material_loss.fragment_loss_m2, 0)::float AS fragment_loss_m2,
                    COALESCE(material_loss.fragment_loss_components, 0)::bigint AS fragment_loss_components
                FROM forest_source
                LEFT JOIN LATERAL (
                    SELECT
                        ST_Area(
                            ST_UnaryUnion(
                                ST_Collect(ST_Intersection(forest_source.geom, final_mask.geom))
                            )
                        )::float AS retained_m2
                    FROM final_mask
                    WHERE final_mask.geom && forest_source.geom
                      AND ST_Intersects(final_mask.geom, forest_source.geom)
                ) retained ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        ST_Area(
                            ST_UnaryUnion(
                                ST_Collect(ST_Intersection(forest_source.geom, exclusions.geom))
                            )
                        )::float AS excluded_m2
                    FROM exclusions
                    WHERE exclusions.geom && forest_source.geom
                      AND ST_Intersects(exclusions.geom, forest_source.geom)
                ) excluded ON TRUE
                LEFT JOIN LATERAL (
                    WITH covered AS (
                        SELECT ST_UnaryUnion(ST_Collect(part.geom)) AS geom
                        FROM (
                            SELECT ST_Intersection(forest_source.geom, final_mask.geom) AS geom
                            FROM final_mask
                            WHERE final_mask.geom && forest_source.geom
                              AND ST_Intersects(final_mask.geom, forest_source.geom)
                            UNION ALL
                            SELECT ST_Intersection(forest_source.geom, exclusions.geom) AS geom
                            FROM exclusions
                            WHERE exclusions.geom && forest_source.geom
                              AND ST_Intersects(exclusions.geom, forest_source.geom)
                        ) part
                        WHERE NOT ST_IsEmpty(part.geom)
                    ),
                    loss_parts AS (
                        SELECT (ST_Dump(
                            ST_MakeValid(
                                CASE
                                    WHEN covered.geom IS NULL THEN forest_source.geom
                                    ELSE ST_Difference(
                                        ST_CollectionExtract(
                                            ST_MakeValid(ST_SnapToGrid(forest_source.geom, 0.001)),
                                            3
                                        ),
                                        ST_CollectionExtract(
                                            ST_MakeValid(ST_SnapToGrid(covered.geom, 0.001)),
                                            3
                                        )
                                    )
                                END
                            )
                        )).geom AS geom
                        FROM covered
                    ),
                    measured AS (
                        SELECT ST_Area(loss_parts.geom)::float AS area_m2
                        FROM loss_parts
                        WHERE NOT ST_IsEmpty(loss_parts.geom)
                    )
                    SELECT
                        COALESCE(sum(area_m2) FILTER (
                            WHERE area_m2 >= (SELECT material_component_area_m2 FROM params)
                        ), 0)::float AS material_loss_m2,
                        count(*) FILTER (
                            WHERE area_m2 >= (SELECT material_component_area_m2 FROM params)
                        )::bigint AS material_loss_components,
                        COALESCE(max(area_m2) FILTER (
                            WHERE area_m2 >= (SELECT material_component_area_m2 FROM params)
                        ), 0)::float AS max_material_loss_m2,
                        COALESCE(sum(area_m2) FILTER (
                            WHERE area_m2 < (SELECT material_component_area_m2 FROM params)
                        ), 0)::float AS fragment_loss_m2,
                        count(*) FILTER (
                            WHERE area_m2 < (SELECT material_component_area_m2 FROM params)
                        )::bigint AS fragment_loss_components
                    FROM measured
                ) material_loss ON TRUE
            ),
            exclusion_by_layer AS (
                SELECT
                    exclusions.source_layer,
                    count(*)::bigint AS components,
                    COALESCE(sum(exclusions.area_m2), 0)::float AS area_m2
                FROM exclusions
                GROUP BY exclusions.source_layer
            ),
            metrics AS (
                SELECT
                    (SELECT count(*) FROM forest_source)::bigint AS forest_source_components,
                    COALESCE((SELECT sum(area_m2) FROM forest_source), 0)::float AS forest_source_area_m2,
                    (SELECT count(*) FROM final_mask)::bigint AS final_mask_components,
                    COALESCE((SELECT sum(area_m2) FROM final_mask), 0)::float AS final_mask_area_m2,
                    (SELECT count(*) FROM exclusions)::bigint AS exclusion_components,
                    COALESCE((SELECT sum(area_m2) FROM exclusions), 0)::float AS exclusion_area_m2,
                    COALESCE(sum(retained_m2), 0)::float AS retained_forest_source_area_m2,
                    COALESCE(sum(excluded_m2), 0)::float AS excluded_forest_source_area_m2,
                    COALESCE(sum(material_loss_m2), 0)::float AS unexplained_removed_area_m2,
                    COALESCE(sum(material_loss_components), 0)::bigint AS unexplained_removed_components,
                    COALESCE(max(max_material_loss_m2), 0)::float AS max_unexplained_removed_m2,
                    COALESCE(sum(fragment_loss_m2), 0)::float AS explained_fragment_removed_area_m2,
                    COALESCE(sum(fragment_loss_components), 0)::bigint AS explained_fragment_removed_components
                FROM forest_component_metrics
            )
            SELECT
                metrics.*,
                global_loss_metrics.global_unexplained_removed_area_m2,
                global_loss_metrics.global_unexplained_removed_components,
                global_loss_metrics.global_max_removed_m2,
                global_loss_metrics.global_max_unexplained_removed_m2,
                global_loss_metrics.global_fragment_removed_area_m2,
                global_loss_metrics.global_fragment_removed_components,
                COALESCE(
                    jsonb_object_agg(
                        exclusion_by_layer.source_layer,
                        jsonb_build_object(
                            'components', exclusion_by_layer.components,
                            'area_ha', round((exclusion_by_layer.area_m2 / 10000.0)::numeric, 4)
                        )
                    ) FILTER (WHERE exclusion_by_layer.source_layer IS NOT NULL),
                    '{}'::jsonb
                ) AS exclusion_layers
            FROM metrics
            CROSS JOIN global_loss_metrics
            LEFT JOIN exclusion_by_layer ON TRUE
            GROUP BY
                metrics.forest_source_components,
                metrics.forest_source_area_m2,
                metrics.final_mask_components,
                metrics.final_mask_area_m2,
                metrics.exclusion_components,
                metrics.exclusion_area_m2,
                metrics.retained_forest_source_area_m2,
                metrics.excluded_forest_source_area_m2,
                metrics.unexplained_removed_area_m2,
                metrics.unexplained_removed_components,
                metrics.max_unexplained_removed_m2,
                metrics.explained_fragment_removed_area_m2,
                metrics.explained_fragment_removed_components,
                global_loss_metrics.global_unexplained_removed_area_m2,
                global_loss_metrics.global_unexplained_removed_components,
                global_loss_metrics.global_max_removed_m2,
                global_loss_metrics.global_max_unexplained_removed_m2,
                global_loss_metrics.global_fragment_removed_area_m2,
                global_loss_metrics.global_fragment_removed_components
            """
        ),
        {
            "region_code": region_code,
            "source_region_code": source_region_code,
            "mask_version": mask_version,
            "forest_source_layer": forest_source_layer,
            "exclusion_source_layers": exclusion_source_layers,
            "projected_srid": projected_srid,
            "min_disagreement_area_ha": min_disagreement_area_ha,
            "max_unexplained_component_ha": max_unexplained_component_ha,
            "use_bbox": bbox is not None,
            "min_lon": bbox[0] if bbox else 0,
            "min_lat": bbox[1] if bbox else 0,
            "max_lon": bbox[2] if bbox else 0,
            "max_lat": bbox[3] if bbox else 0,
            "component_limit": component_limit,
            "component_offset": component_offset,
        },
    ).mappings().one()

    forest_source_area_m2 = float(row["forest_source_area_m2"] or 0)
    unexplained_removed_area_m2 = float(row["unexplained_removed_area_m2"] or 0)
    unexplained_ratio = (
        unexplained_removed_area_m2 / forest_source_area_m2
        if forest_source_area_m2
        else 0.0
    )
    global_unexplained_removed_area_m2 = float(_row_value(row, "global_unexplained_removed_area_m2") or 0)
    global_unexplained_ratio = (
        global_unexplained_removed_area_m2 / forest_source_area_m2
        if forest_source_area_m2
        else 0.0
    )
    checks = {
        "full_source_scope": component_limit == 0 and component_offset == 0 and bbox is None,
        "has_forest_source": int(row["forest_source_components"] or 0) > 0,
        "has_final_mask": int(row["final_mask_components"] or 0) > 0,
        "has_exclusion_sources": int(row["exclusion_components"] or 0) > 0,
        "unexplained_source_loss_within_ratio_budget": unexplained_ratio <= max_unexplained_ratio,
        "unexplained_source_loss_within_component_budget": (
            float(row["max_unexplained_removed_m2"] or 0) / 10000.0
        )
        < max_unexplained_component_ha,
        "global_unexplained_source_loss_within_ratio_budget": global_unexplained_ratio <= max_unexplained_ratio,
        "global_unexplained_source_loss_within_component_budget": (
            float(_row_value(row, "global_max_unexplained_removed_m2") or 0) / 10000.0
        )
        < max_unexplained_component_ha,
    }
    checks["ready"] = all(checks.values())

    return {
        "region_code": region_code,
        "source_region_code": source_region_code,
        "mask_version": mask_version,
        "forest_source_layer": forest_source_layer,
        "exclusion_source_layers": exclusion_source_layers,
        "projected_srid": projected_srid,
        "bbox": bbox,
        "component_limit": component_limit,
        "component_offset": component_offset,
        "min_disagreement_area_ha": min_disagreement_area_ha,
        "max_unexplained_ratio": max_unexplained_ratio,
        "max_unexplained_component_ha": max_unexplained_component_ha,
        "forest_source_components": int(row["forest_source_components"] or 0),
        "forest_source_area_ha": round(forest_source_area_m2 / 10000.0, 4),
        "final_mask_components": int(row["final_mask_components"] or 0),
        "final_mask_area_ha": round(float(row["final_mask_area_m2"] or 0) / 10000.0, 4),
        "exclusion_components": int(row["exclusion_components"] or 0),
        "exclusion_area_ha": round(float(row["exclusion_area_m2"] or 0) / 10000.0, 4),
        "exclusion_layers": row["exclusion_layers"],
        "retained_forest_source_area_ha": round(float(row["retained_forest_source_area_m2"] or 0) / 10000.0, 4),
        "excluded_forest_source_area_ha": round(float(row["excluded_forest_source_area_m2"] or 0) / 10000.0, 4),
        "unexplained_removed_area_ha": round(unexplained_removed_area_m2 / 10000.0, 4),
        "unexplained_removed_ratio": round(unexplained_ratio, 8),
        "unexplained_removed_components": int(row["unexplained_removed_components"] or 0),
        "max_unexplained_removed_ha": round(float(row["max_unexplained_removed_m2"] or 0) / 10000.0, 4),
        "global_unexplained_removed_area_ha": round(global_unexplained_removed_area_m2 / 10000.0, 4),
        "global_unexplained_removed_ratio": round(global_unexplained_ratio, 8),
        "global_unexplained_removed_components": int(_row_value(row, "global_unexplained_removed_components") or 0),
        "global_max_unexplained_removed_ha": round(
            float(_row_value(row, "global_max_unexplained_removed_m2") or 0) / 10000.0,
            4,
        ),
        "global_max_removed_ha": round(float(_row_value(row, "global_max_removed_m2") or 0) / 10000.0, 4),
        "global_fragment_removed_area_ha": round(
            float(_row_value(row, "global_fragment_removed_area_m2") or 0) / 10000.0,
            4,
        ),
        "global_fragment_removed_components": int(_row_value(row, "global_fragment_removed_components") or 0),
        "explained_fragment_removed_area_ha": round(
            float(row.get("explained_fragment_removed_area_m2", 0) or 0) / 10000.0,
            4,
        ),
        "explained_fragment_removed_components": int(
            row.get("explained_fragment_removed_components", 0) or 0
        ),
        "checks": checks,
    }


def _parse_bbox(value: str) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [float(part.strip()) for part in value.split(",") if part.strip()]
    if len(parts) != 4:
        raise ValueError("bbox must be lon_min,lat_min,lon_max,lat_max")
    min_lon, min_lat, max_lon, max_lat = parts
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox min values must be smaller than max values")
    return min_lon, min_lat, max_lon, max_lat


def _row_value(row: Any, key: str, default: Any = 0) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


if __name__ == "__main__":
    main()
