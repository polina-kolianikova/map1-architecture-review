from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.forest_scope import (
    validate_auxiliary_nonforest_exclusion_source_layers,
    validate_forest_mask_source_layers,
)
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG,
    STAGE3_PUBLIC_V9_PROJECTED_SRID,
    require_stage3_public_v9_full_source_cache_args,
    require_user_approved_full_turkey,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize valid projected public source-mask geometry for Stage 3 grid generation"
    )
    parser.add_argument("--source-region-code", required=True)
    parser.add_argument("--source-layers", required=True, help="comma-separated public mask/exclusion source layers")
    parser.add_argument("--projected-srid", type=int, required=True)
    parser.add_argument("--max-vertices", type=int, default=256)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--append", action="store_true", help="append a disjoint source-id shard to an existing cache")
    parser.add_argument(
        "--replace-shard",
        action="store_true",
        help="delete and rebuild only the selected source-id shard before appending it",
    )
    parser.add_argument("--source-id-modulo", type=int, default=0)
    parser.add_argument("--source-id-remainder", type=int, default=0)
    parser.add_argument(
        "--min-forest-component-area-ha",
        type=float,
        default=0,
        help=(
            "drop source forest-mask components smaller than this area; non-forest "
            "exclusion layers are never filtered by this option"
        ),
    )
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument(
        STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG,
        action="store_true",
        help="required before replacing/preparing national full Turkey V10 source cache",
    )
    args = parser.parse_args()

    source_layers = _parse_source_layers(args.source_layers)
    _validate_cache_source_layers(source_layers)
    if args.max_vertices < 5:
        raise ValueError("max_vertices must be >= 5")
    if args.min_forest_component_area_ha < 0:
        raise ValueError("min-forest-component-area-ha must not be negative")
    _validate_source_id_shard(args.source_id_modulo, args.source_id_remainder)

    with SessionLocal() as db:
        scope_area_ha = _region_area_ha(db, args.source_region_code)
        require_stage3_public_v9_full_source_cache_args(
            source_region_code=args.source_region_code,
            source_layers=source_layers,
            projected_srid=args.projected_srid,
            min_forest_component_area_ha=args.min_forest_component_area_ha,
            scope_area_ha=scope_area_ha,
        )
        require_user_approved_full_turkey(
            approved=args.ack_full_turkey_user_approved,
            source_region_code=args.source_region_code,
            clip_region_code=args.source_region_code,
            scope_area_ha=scope_area_ha,
        )
        existing = _cache_counts(db, args.source_region_code, source_layers, args.projected_srid)
        if existing and not args.replace and not args.append and not args.replace_shard:
            raise RuntimeError("Stage 3 source mask cache already exists for this scope; use --replace")
        source_counts = _source_counts(
            db,
            args.source_region_code,
            source_layers,
            source_id_modulo=args.source_id_modulo,
            source_id_remainder=args.source_id_remainder,
        )
        missing_layers = [layer for layer in source_layers if source_counts.get(layer, 0) <= 0]
        if missing_layers:
            raise RuntimeError(f"Missing source features for cache layer(s): {', '.join(missing_layers)}")

        # Keep the old cache visible until the replacement and its report succeed.
        # Session close rolls back both the deletion and insertion on any failure.
        if args.replace:
            _delete_cache(db, args.source_region_code, source_layers, args.projected_srid)
        elif args.replace_shard:
            if args.source_id_modulo <= 0:
                raise RuntimeError("--replace-shard requires --source-id-modulo > 0")
            _delete_cache_shard(
                db,
                args.source_region_code,
                source_layers,
                args.projected_srid,
                args.source_id_modulo,
                args.source_id_remainder,
            )
        elif args.append and args.source_id_modulo > 0:
            if _cache_shard_exists(
                db,
                args.source_region_code,
                source_layers,
                args.projected_srid,
                args.source_id_modulo,
                args.source_id_remainder,
            ):
                raise RuntimeError(
                    "Stage 3 source mask cache shard already exists for this scope; use --replace-shard"
                )

        inserted = _insert_cache(
            db,
            source_region_code=args.source_region_code,
            source_layers=source_layers,
            projected_srid=args.projected_srid,
            max_vertices=args.max_vertices,
            source_id_modulo=args.source_id_modulo,
            source_id_remainder=args.source_id_remainder,
            min_forest_component_area_ha=args.min_forest_component_area_ha,
        )
        if args.analyze:
            db.execute(text("ANALYZE stage3_source_mask_cache"))
        report = {
            "source_region_code": args.source_region_code,
            "source_layers": source_layers,
            "projected_srid": args.projected_srid,
            "max_vertices": args.max_vertices,
            "source_features": source_counts,
            "source_id_shard": {
                "modulo": args.source_id_modulo,
                "remainder": args.source_id_remainder,
                "replace_shard": bool(args.replace_shard),
            },
            "clean_mask_policy": _cache_clean_mask_policy(args.min_forest_component_area_ha),
            "cache_components_inserted": inserted,
            "cache_counts": _cache_counts(db, args.source_region_code, source_layers, args.projected_srid),
            "cache_quality": _cache_quality(db, args.source_region_code, source_layers, args.projected_srid),
        }
        db.commit()

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _parse_source_layers(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = value.split(",")
    return [str(item).strip() for item in raw if str(item).strip()]


def _validate_cache_source_layers(source_layers: list[str]) -> None:
    if not source_layers:
        raise RuntimeError("Stage 3 source mask cache source layers are not configured")
    invalid_layers = []
    for source_layer in source_layers:
        if _is_valid_forest_source_layer(source_layer) or _is_valid_nonforest_exclusion_layer(source_layer):
            continue
        invalid_layers.append(source_layer)
    if invalid_layers:
        raise RuntimeError(f"Stage 3 source mask cache layer is not allowed: {', '.join(invalid_layers)}")


def _validate_source_id_shard(source_id_modulo: int, source_id_remainder: int) -> None:
    if source_id_modulo < 0:
        raise ValueError("source_id_modulo must be >= 0")
    if source_id_modulo == 0:
        if source_id_remainder != 0:
            raise ValueError("source_id_remainder must be 0 when source_id_modulo is 0")
        return
    if source_id_remainder < 0 or source_id_remainder >= source_id_modulo:
        raise ValueError("source_id_remainder must be >= 0 and < source_id_modulo")


def _cache_clean_mask_policy(min_forest_component_area_ha: float) -> dict[str, Any]:
    if min_forest_component_area_ha <= 0:
        return {}
    return {
        "method": "forest_source_component_area_filter_before_cache_insert",
        "min_forest_component_area_ha": float(min_forest_component_area_ha),
        "nonforest_exclusions_filtered": False,
    }


def _is_valid_forest_source_layer(source_layer: str) -> bool:
    try:
        validate_forest_mask_source_layers([source_layer])
        return True
    except RuntimeError:
        return False


def _is_valid_nonforest_exclusion_layer(source_layer: str) -> bool:
    try:
        validate_auxiliary_nonforest_exclusion_source_layers([source_layer])
        return True
    except RuntimeError:
        return False


def _source_counts(
    db,
    source_region_code: str,
    source_layers: list[str],
    *,
    source_id_modulo: int = 0,
    source_id_remainder: int = 0,
) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT source_layer, count(*)::bigint AS count
            FROM ogm_parcels
            WHERE is_current
              AND attributes->>'region_code' = :source_region_code
              AND source_layer = ANY(:source_layers)
              AND (
                    :source_id_modulo <= 0
                    OR mod(id, :source_id_modulo) = :source_id_remainder
                  )
            GROUP BY source_layer
            """
        ),
        {
            "source_region_code": source_region_code,
            "source_layers": source_layers,
            "source_id_modulo": source_id_modulo,
            "source_id_remainder": source_id_remainder,
        },
    ).mappings().all()
    counts = {str(row["source_layer"]): int(row["count"]) for row in rows}
    return {source_layer: counts.get(source_layer, 0) for source_layer in source_layers}


def _cache_counts(db, source_region_code: str, source_layers: list[str], projected_srid: int) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT source_layer, count(*)::bigint AS count
            FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
            GROUP BY source_layer
            """
        ),
        {
            "source_region_code": source_region_code,
            "projected_srid": projected_srid,
            "source_layers": source_layers,
        },
    ).mappings().all()
    counts = {str(row["source_layer"]): int(row["count"]) for row in rows}
    return {source_layer: counts.get(source_layer, 0) for source_layer in source_layers}


def _cache_quality(db, source_region_code: str, source_layers: list[str], projected_srid: int) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
                count(*)::bigint AS components,
                count(*) FILTER (WHERE NOT ST_IsValid(geom_4326))::bigint AS invalid_4326,
                count(*) FILTER (WHERE NOT ST_IsValid(geom_projected))::bigint AS invalid_projected,
                count(*) FILTER (WHERE ST_IsEmpty(geom_4326) OR ST_IsEmpty(geom_projected))::bigint AS empty_components,
                round((sum(ST_Area(geom_projected)) / 10000.0)::numeric, 4) AS area_ha
            FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
            """
        ),
        {
            "source_region_code": source_region_code,
            "projected_srid": projected_srid,
            "source_layers": source_layers,
        },
    ).mappings().one()
    return {
        "components": int(row["components"] or 0),
        "invalid_4326": int(row["invalid_4326"] or 0),
        "invalid_projected": int(row["invalid_projected"] or 0),
        "empty_components": int(row["empty_components"] or 0),
        "area_ha": float(row["area_ha"] or 0),
    }


def _region_area_ha(db, region_code: str) -> float:
    return float(
        db.execute(
            text(
                """
                SELECT ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha
                FROM regions
                WHERE code = :region_code
                """
            ),
            {
                "region_code": region_code,
                "projected_srid": STAGE3_PUBLIC_V9_PROJECTED_SRID,
            },
        ).scalar_one()
        or 0
    )


def _delete_cache(db, source_region_code: str, source_layers: list[str], projected_srid: int) -> None:
    db.execute(
        text(
            """
            DELETE FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
            """
        ),
        {
            "source_region_code": source_region_code,
            "projected_srid": projected_srid,
            "source_layers": source_layers,
        },
    )


def _delete_cache_shard(
    db,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
    source_id_modulo: int,
    source_id_remainder: int,
) -> None:
    db.execute(
        text(
            """
            DELETE FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
              AND metadata->>'source_id_modulo' = :source_id_modulo
              AND metadata->>'source_id_remainder' = :source_id_remainder
            """
        ),
        {
            "source_region_code": source_region_code,
            "projected_srid": projected_srid,
            "source_layers": source_layers,
            "source_id_modulo": str(source_id_modulo),
            "source_id_remainder": str(source_id_remainder),
        },
    )


def _cache_shard_exists(
    db,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
    source_id_modulo: int,
    source_id_remainder: int,
) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM stage3_source_mask_cache
                    WHERE source_region_code = :source_region_code
                      AND projected_srid = :projected_srid
                      AND source_layer = ANY(:source_layers)
                      AND metadata->>'source_id_modulo' = :source_id_modulo
                      AND metadata->>'source_id_remainder' = :source_id_remainder
                )
                """
            ),
            {
                "source_region_code": source_region_code,
                "projected_srid": projected_srid,
                "source_layers": source_layers,
                "source_id_modulo": str(source_id_modulo),
                "source_id_remainder": str(source_id_remainder),
            },
        ).scalar_one()
    )


def _insert_cache(
    db,
    *,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
    max_vertices: int,
    source_id_modulo: int = 0,
    source_id_remainder: int = 0,
    min_forest_component_area_ha: float = 0,
) -> int:
    component_area_filtered_layers = [
        source_layer for source_layer in source_layers if not _is_valid_nonforest_exclusion_layer(source_layer)
    ]
    return int(
        db.execute(
            text(
                """
                WITH source_region AS (
                    SELECT ST_MakeValid(geom) AS geom
                    FROM regions
                    WHERE code = :source_region_code
                ),
                scoped AS (
                    SELECT
                        parcel.id AS source_parcel_id,
                        parcel.source_layer,
                        ST_Intersection(ST_MakeValid(parcel.geom), source_region.geom) AS source_geom
                    FROM ogm_parcels parcel
                    JOIN source_region
                      ON source_region.geom IS NOT NULL
                     AND NOT ST_IsEmpty(source_region.geom)
                     AND parcel.geom && source_region.geom
                     AND ST_Intersects(ST_MakeValid(parcel.geom), source_region.geom)
                    WHERE parcel.is_current
                      AND parcel.attributes->>'region_code' = :source_region_code
                      AND parcel.source_layer = ANY(:source_layers)
                      AND (
                            :source_id_modulo <= 0
                            OR mod(parcel.id, :source_id_modulo) = :source_id_remainder
                          )
                ),
                valid_components AS (
                    SELECT
                        scoped.source_parcel_id,
                        scoped.source_layer,
                        component_geom.source_component_index,
                        component_geom.geom_4326,
                        ST_Area(ST_Transform(component_geom.geom_4326, :projected_srid)) / 10000.0
                            AS source_component_area_ha
                    FROM scoped
                    CROSS JOIN LATERAL (
                        SELECT
                            component.path[1]::integer AS source_component_index,
                            ST_Multi(
                                ST_CollectionExtract(
                                    ST_MakeValid(component.geom),
                                    3
                                )
                            )::geometry(Geometry, 4326) AS geom_4326
                        FROM ST_Dump(
                            ST_CollectionExtract(ST_MakeValid(scoped.source_geom), 3)
                        ) AS component
                    ) AS component_geom
                    WHERE component_geom.geom_4326 IS NOT NULL
                      AND NOT ST_IsEmpty(component_geom.geom_4326)
                      AND (
                          :min_forest_component_area_ha <= 0
                          OR NOT (scoped.source_layer = ANY(:component_area_filtered_layers))
                          OR ST_Area(ST_Transform(component_geom.geom_4326, :projected_srid)) / 10000.0
                             >= :min_forest_component_area_ha
                      )
                ),
                subdivided AS (
                    SELECT
                        source_parcel_id,
                        source_layer,
                        source_component_index,
                        ST_Multi(
                            ST_CollectionExtract(
                                ST_MakeValid(part.geom),
                                3
                            )
                        )::geometry(Geometry, 4326) AS geom_4326
                    FROM valid_components
                    CROSS JOIN LATERAL ST_Subdivide(valid_components.geom_4326, :max_vertices) AS part(geom)
                    WHERE valid_components.geom_4326 IS NOT NULL
                      AND NOT ST_IsEmpty(valid_components.geom_4326)
                ),
                valid_subdivided AS (
                    SELECT
                        source_parcel_id,
                        source_layer,
                        source_component_index,
                        geom_4326,
                        ST_Transform(geom_4326, :projected_srid)::geometry(Geometry) AS geom_projected
                    FROM subdivided
                    WHERE geom_4326 IS NOT NULL
                      AND NOT ST_IsEmpty(geom_4326)
                ),
                numbered AS (
                    SELECT
                        source_parcel_id,
                        source_layer,
                        geom_4326,
                        geom_projected,
                        row_number() OVER (
                            PARTITION BY source_parcel_id
                            ORDER BY source_component_index, ST_Area(geom_projected) DESC
                        )::integer AS component_index
                    FROM valid_subdivided
                    WHERE geom_projected IS NOT NULL
                      AND NOT ST_IsEmpty(geom_projected)
                      AND ST_Area(geom_projected) > 0
                ),
                inserted AS (
                    INSERT INTO stage3_source_mask_cache (
                        source_region_code,
                        source_layer,
                        projected_srid,
                        source_parcel_id,
                        component_index,
                        geom_4326,
                        geom_projected,
                        geom_hash,
                        area_ha,
                        metadata
                    )
                    SELECT
                        :source_region_code,
                        source_layer,
                        :projected_srid,
                        source_parcel_id,
                        component_index,
                        geom_4326,
                        geom_projected,
                        md5(
                            source_parcel_id::text
                            || ':'
                            || component_index::text
                            || ':'
                            || encode(ST_AsEWKB(geom_projected), 'hex')
                        ),
                        round((ST_Area(geom_projected) / 10000.0)::numeric, 4),
                        jsonb_build_object(
                            'pipeline', 'stage3_source_mask_cache',
                            'clipped_to_source_region', true,
                            'max_vertices', :max_vertices,
                            'source_id_modulo', :source_id_modulo,
                            'source_id_remainder', :source_id_remainder,
                            'clean_mask_policy', CAST(:clean_mask_policy AS jsonb)
                        )
                    FROM numbered
                    RETURNING 1
                )
                SELECT count(*)::bigint FROM inserted
                """
            ),
            {
                "source_region_code": source_region_code,
                "source_layers": source_layers,
                "projected_srid": projected_srid,
                "max_vertices": max_vertices,
                "source_id_modulo": source_id_modulo,
                "source_id_remainder": source_id_remainder,
                "min_forest_component_area_ha": min_forest_component_area_ha,
                "component_area_filtered_layers": component_area_filtered_layers,
                "clean_mask_policy": json.dumps(
                    _cache_clean_mask_policy(min_forest_component_area_ha),
                    separators=(",", ":"),
                ),
            },
        ).scalar_one()
    )


if __name__ == "__main__":
    main()
