from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.forest_scope import (
    forest_mask_source_layers,
    forest_region_metadata,
    manual_region_metadata,
    validate_auxiliary_forest_mask_source_layers,
    validate_auxiliary_nonforest_exclusion_source_layers,
    validate_forest_mask_source_layers,
)
from app.services.source_strategy import SOURCE_LAYER_MESCERE
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_PROJECTED_SRID,
    STAGE3_PUBLIC_V9_REGION_CODE,
    is_stage3_public_v9_full_scope,
    require_user_approved_full_turkey,
)


TURKEY_BBOX = (25.0, 35.5, 45.2, 42.5)
TILED_COVERAGE_UNION_TILE_SIZE_M = 10_000
COMPONENT_SUBDIVIDE_MAX_VERTICES = 256
COMPONENT_UNION_BATCH_SIZE = 8
COMPONENT_UNION_FINAL_TARGET_ROWS = 1
COMPONENT_UNION_MAX_LEVELS = 24


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or refine a Stage 3 region geometry")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--country-code", default="TR")
    parser.add_argument("--bbox", default="", help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--turkey-bbox", action="store_true")
    parser.add_argument("--wkt-file", default="")
    parser.add_argument("--from-layer-a", action="store_true")
    parser.add_argument("--source-region-code", default="")
    parser.add_argument(
        "--source-layers",
        default="",
        help="comma-separated primary Layer A mask layers for --from-layer-a",
    )
    parser.add_argument(
        "--intersect-mask-source-layers",
        default="",
        help="comma-separated auxiliary mask layers to intersect with dissolved Layer A",
    )
    parser.add_argument(
        "--difference-mask-source-layers",
        default="",
        help="comma-separated auxiliary non-forest layers to subtract from the forest mask",
    )
    parser.add_argument("--clip-to-grid-version", default="")
    parser.add_argument("--min-area-ha", type=float, default=0)
    parser.add_argument("--max-area-ha", type=float, default=0)
    parser.add_argument(
        "--min-mask-component-area-ha",
        type=float,
        default=0,
        help=(
            "drop isolated final forest-mask components smaller than this area before "
            "saving the Stage 3 region geometry"
        ),
    )
    parser.add_argument("--projected-srid", type=int, default=32636)
    parser.add_argument("--union-tile-size-m", type=int, default=TILED_COVERAGE_UNION_TILE_SIZE_M)
    parser.add_argument(
        "--use-source-mask-cache",
        action="store_true",
        help="use stage3_source_mask_cache projected components for public mask/exclusion dissolve",
    )
    parser.add_argument(
        "--ack-full-turkey-user-approved",
        action="store_true",
        help="required for guarded full Turkey seed preparation after explicit user approval",
    )
    args = parser.parse_args()
    _validate_args(args)

    with SessionLocal() as db:
        if args.from_layer_a:
            source_region_code = args.source_region_code or args.region_code
            source_layers = parse_source_layers(args.source_layers)
            intersect_layers = parse_source_layers(args.intersect_mask_source_layers)
            difference_layers = parse_source_layers(args.difference_mask_source_layers)
            _guard_legacy_layer_a_full_scope(
                approved=args.ack_full_turkey_user_approved,
                region_code=args.region_code,
                source_region_code=source_region_code,
                scope_area_ha=_existing_region_area_ha(db, source_region_code),
            )
            result = _upsert_region_from_layer_a_with_mask(
                db,
                region_code=args.region_code,
                region_name=args.region_name or args.region_code,
                country_code=args.country_code,
                source_region_code=source_region_code,
                source_layers=source_layers,
                intersect_source_layers=intersect_layers,
                difference_source_layers=difference_layers,
                clip_to_grid_version=args.clip_to_grid_version,
                min_mask_component_area_ha=args.min_mask_component_area_ha,
                projected_srid=args.projected_srid,
                union_tile_size_m=args.union_tile_size_m,
                use_source_mask_cache=args.use_source_mask_cache,
            )
        else:
            wkt = _load_wkt(args)
            source = _manual_source(args)
            _guard_legacy_manual_full_scope(
                approved=args.ack_full_turkey_user_approved,
                region_code=args.region_code,
                scope_area_ha=_wkt_area_ha(db, wkt),
            )
            result = _upsert_region_from_wkt(
                db,
                region_code=args.region_code,
                region_name=args.region_name or args.region_code,
                country_code=args.country_code,
                wkt=wkt,
                projected_srid=args.projected_srid,
                source=source,
            )
        _validate_area(result["area_ha"], args.min_area_ha, args.max_area_ha)
        db.commit()

    print(
        "stage3 region ready "
        f"region_code={result['code']} "
        f"area_ha={round(result['area_ha'], 2)} "
        f"source={result['source']}"
    )


def _validate_args(args) -> None:
    modes = sum(bool(value) for value in (args.bbox, args.turkey_bbox, args.wkt_file, args.from_layer_a))
    if modes != 1:
        raise ValueError("choose exactly one of --bbox, --turkey-bbox, --wkt-file, or --from-layer-a")
    if args.min_area_ha < 0:
        raise ValueError("min_area_ha must not be negative")
    if args.max_area_ha < 0:
        raise ValueError("max_area_ha must not be negative")
    if args.max_area_ha and args.min_area_ha and args.max_area_ha < args.min_area_ha:
        raise ValueError("max_area_ha must be greater than or equal to min_area_ha")
    if getattr(args, "min_mask_component_area_ha", 0) < 0:
        raise ValueError("min-mask-component-area-ha must not be negative")
    if args.projected_srid <= 0:
        raise ValueError("projected_srid must be greater than zero")
    if getattr(args, "union_tile_size_m", TILED_COVERAGE_UNION_TILE_SIZE_M) <= 0:
        raise ValueError("union-tile-size-m must be greater than zero")
    if args.clip_to_grid_version and not args.from_layer_a:
        raise ValueError("clip-to-grid-version can only be used with --from-layer-a")
    if getattr(args, "source_layers", "") and not args.from_layer_a:
        raise ValueError("source-layers can only be used with --from-layer-a")
    if args.intersect_mask_source_layers and not args.from_layer_a:
        raise ValueError("intersect-mask-source-layers can only be used with --from-layer-a")
    if args.difference_mask_source_layers and not args.from_layer_a:
        raise ValueError("difference-mask-source-layers can only be used with --from-layer-a")
    if args.intersect_mask_source_layers and args.clip_to_grid_version:
        raise ValueError("intersect-mask-source-layers cannot be combined with clip-to-grid-version")
    if args.use_source_mask_cache and not args.from_layer_a:
        raise ValueError("use-source-mask-cache can only be used with --from-layer-a")


def _guard_legacy_manual_full_scope(
    *,
    approved: bool,
    region_code: str,
    scope_area_ha: float | None,
) -> None:
    if region_code == STAGE3_PUBLIC_V9_REGION_CODE:
        raise RuntimeError(
            "Manual/bbox Stage 3 region preparation cannot write the final Turkey V10 client mask; "
            "use app.commands.run_stage3_prepare_region_fragments with the exact client formula."
        )
    require_user_approved_full_turkey(
        approved=approved,
        region_code=region_code,
        source_region_code=region_code,
        clip_region_code=region_code,
        scope_area_ha=scope_area_ha,
    )


def _guard_legacy_layer_a_full_scope(
    *,
    approved: bool,
    region_code: str,
    source_region_code: str,
    scope_area_ha: float | None,
) -> None:
    if not is_stage3_public_v9_full_scope(
        region_code=region_code,
        source_region_code=source_region_code,
        clip_region_code=source_region_code,
        scope_area_ha=scope_area_ha,
    ):
        return
    require_user_approved_full_turkey(
        approved=approved,
        region_code=region_code,
        source_region_code=source_region_code,
        clip_region_code=source_region_code,
        scope_area_ha=scope_area_ha,
    )
    raise RuntimeError(
        "Legacy Stage 3 prepare_region --from-layer-a is disabled for full/national Turkey V10 scope; "
        "use app.commands.run_stage3_prepare_region_fragments with "
        "ogm_esatis_bolme ∩ osm_forest_mask − osm_nonforest_exclusion and final components >= 1 ha."
    )


def _existing_region_area_ha(db, region_code: str) -> float | None:
    row = db.execute(
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
    ).scalar_one_or_none()
    return float(row) if row is not None else None


def _wkt_area_ha(db, wkt: str) -> float:
    return float(
        db.execute(
            text(
                """
                SELECT ST_Area(
                    ST_Transform(
                        ST_Multi(
                            ST_CollectionExtract(
                                ST_MakeValid(ST_GeomFromText(:wkt, 4326)),
                                3
                            )
                        )::geometry(MultiPolygon, 4326),
                        :projected_srid
                    )
                ) / 10000.0 AS area_ha
                """
            ),
            {
                "wkt": wkt,
                "projected_srid": STAGE3_PUBLIC_V9_PROJECTED_SRID,
            },
        ).scalar_one()
        or 0
    )


def _load_wkt(args) -> str:
    if args.wkt_file:
        wkt = Path(args.wkt_file).read_text(encoding="utf-8").strip()
        if not wkt:
            raise ValueError("region WKT file is empty")
        return wkt
    bbox = TURKEY_BBOX if args.turkey_bbox else parse_bbox(args.bbox)
    return bbox_to_wkt(bbox)


def _manual_source(args) -> str:
    if args.wkt_file:
        return "wkt"
    if args.turkey_bbox:
        return "turkey_bbox"
    return "bbox"


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be min_lon,min_lat,max_lon,max_lat")
    min_lon, min_lat, max_lon, max_lat = [float(part) for part in parts]
    if not (-180 <= min_lon < max_lon <= 180):
        raise ValueError("bbox longitude range is invalid")
    if not (-90 <= min_lat < max_lat <= 90):
        raise ValueError("bbox latitude range is invalid")
    return min_lon, min_lat, max_lon, max_lat


def bbox_to_wkt(bbox: tuple[float, float, float, float]) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        "POLYGON(("
        f"{min_lon} {min_lat},"
        f"{max_lon} {min_lat},"
        f"{max_lon} {max_lat},"
        f"{min_lon} {max_lat},"
        f"{min_lon} {min_lat}"
        "))"
    )


def parse_source_layers(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _upsert_region_from_wkt(
    db,
    *,
    region_code: str,
    region_name: str,
    country_code: str,
    wkt: str,
    projected_srid: int,
    source: str = "wkt",
) -> dict:
    metadata = manual_region_metadata(source)
    return dict(
        db.execute(
            text(
                """
                WITH prepared AS (
                    SELECT ST_Multi(
                        ST_CollectionExtract(ST_MakeValid(ST_GeomFromText(:wkt, 4326)), 3)
                    )::geometry(MultiPolygon, 4326) AS geom
                ),
                changed AS (
                    INSERT INTO regions (name, code, country_code, geom, metadata)
                    SELECT :name, :code, :country_code, geom, CAST(:metadata AS jsonb)
                    FROM prepared
                    ON CONFLICT (code) DO UPDATE
                    SET name = EXCLUDED.name,
                        country_code = EXCLUDED.country_code,
                        geom = EXCLUDED.geom,
                        metadata = EXCLUDED.metadata
                    RETURNING code, name, country_code, geom, metadata
                )
                SELECT
                    code,
                    name,
                    country_code,
                    ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha,
                    metadata->>'stage3_region_source' AS source
                FROM changed
                """
            ),
            {
                "name": region_name,
                "code": region_code,
                "country_code": country_code,
                "wkt": wkt,
                "projected_srid": projected_srid,
                "metadata": json.dumps(metadata, separators=(",", ":")),
            },
        ).mappings().one()
    )


def _upsert_region_from_layer_a_with_mask(
    db,
    *,
    region_code: str,
    region_name: str,
    country_code: str,
    source_region_code: str,
    intersect_source_layers: list[str],
    difference_source_layers: list[str],
    clip_to_grid_version: str,
    projected_srid: int,
    min_mask_component_area_ha: float = 0,
    source_layers: list[str] | None = None,
    union_tile_size_m: int = TILED_COVERAGE_UNION_TILE_SIZE_M,
    use_source_mask_cache: bool = False,
) -> dict:
    source_layers = source_layers or forest_mask_source_layers()
    validate_forest_mask_source_layers(source_layers)
    if intersect_source_layers:
        validate_auxiliary_forest_mask_source_layers(intersect_source_layers)
    if difference_source_layers:
        validate_auxiliary_nonforest_exclusion_source_layers(difference_source_layers)
    metadata = forest_region_metadata(
        source_region_code=source_region_code,
        source_layers=source_layers,
        intersect_source_layers=intersect_source_layers,
        difference_source_layers=difference_source_layers,
        clip_to_grid_version=clip_to_grid_version,
        min_mask_component_area_ha=min_mask_component_area_ha,
    )
    if _can_use_staged_source_union(
        source_layers=source_layers,
        intersect_source_layers=intersect_source_layers,
        difference_source_layers=difference_source_layers,
        clip_to_grid_version=clip_to_grid_version,
    ):
        return _upsert_region_from_staged_source_union(
            db,
            region_code=region_code,
            region_name=region_name,
            country_code=country_code,
            source_region_code=source_region_code,
            source_layers=source_layers,
            intersect_source_layers=intersect_source_layers,
            difference_source_layers=difference_source_layers,
            projected_srid=projected_srid,
            union_tile_size_m=union_tile_size_m,
            min_mask_component_area_ha=min_mask_component_area_ha,
            use_source_mask_cache=use_source_mask_cache,
            metadata=metadata,
        )
    row = db.execute(
        text(
            """
            WITH layer_a_boundary AS (
                SELECT ST_MemUnion(ST_MakeValid(geom)) AS geom
                FROM ogm_parcels
                WHERE is_current
                  AND source_layer = ANY(:forest_mask_source_layers)
                  AND attributes->>'region_code' = :source_region_code
            ),
            layer_a_forest_class AS (
                SELECT ST_MemUnion(ST_MakeValid(geom)) AS geom
                FROM ogm_parcels
                WHERE :use_mescere_forest_class
                  AND is_current
                  AND source_layer = 'ogm_yayin_mescere_wms_svg'
                  AND attributes->>'region_code' = :source_region_code
                  AND upper(COALESCE(
                      forest_type,
                      attributes->'source_attributes'->>'MESCERE',
                      ''
                  )) = 'F'
            ),
            intersect_mask AS (
                SELECT ST_MemUnion(ST_MakeValid(geom)) AS geom
                FROM ogm_parcels
                WHERE :use_intersect_mask
                  AND is_current
                  AND source_layer = ANY(:intersect_source_layers)
                  AND attributes->>'region_code' = :source_region_code
            ),
            forest_candidates AS (
                SELECT ST_MemUnion(geom) AS geom
                FROM (
                    SELECT geom
                    FROM layer_a_forest_class
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                    UNION ALL
                    SELECT ST_Intersection(layer_a_boundary.geom, intersect_mask.geom) AS geom
                    FROM layer_a_boundary
                    JOIN intersect_mask ON :use_intersect_mask
                    WHERE layer_a_boundary.geom IS NOT NULL
                      AND intersect_mask.geom IS NOT NULL
                      AND NOT ST_IsEmpty(layer_a_boundary.geom)
                      AND NOT ST_IsEmpty(intersect_mask.geom)
                    UNION ALL
                    SELECT layer_a_boundary.geom
                    FROM layer_a_boundary
                    WHERE layer_a_boundary.geom IS NOT NULL
                      AND NOT ST_IsEmpty(layer_a_boundary.geom)
                      AND NOT :use_intersect_mask
                      AND NOT EXISTS (
                          SELECT 1
                          FROM layer_a_forest_class
                          WHERE geom IS NOT NULL
                            AND NOT ST_IsEmpty(geom)
                      )
                ) candidate
                WHERE candidate.geom IS NOT NULL
                  AND NOT ST_IsEmpty(candidate.geom)
            ),
            difference_mask AS (
                SELECT ST_MemUnion(ST_MakeValid(geom)) AS geom
                FROM ogm_parcels
                WHERE :use_difference_mask
                  AND is_current
                  AND source_layer = ANY(:difference_source_layers)
                  AND attributes->>'region_code' = :source_region_code
            ),
            grid_clip AS (
                SELECT ST_MemUnion(cell.geom) AS geom
                FROM grid_cells cell
                JOIN regions region ON region.id = cell.region_id
                WHERE region.code = :source_region_code
                  AND cell.grid_version = :clip_to_grid_version
            ),
            mask_base AS (
                SELECT
                    CASE
                        WHEN :use_difference_mask AND difference_mask.geom IS NOT NULL
                        THEN ST_Difference(
                            ST_MakeValid(ST_Intersection(layer_a_boundary.geom, forest_candidates.geom)),
                            difference_mask.geom
                        )
                        ELSE ST_Intersection(layer_a_boundary.geom, forest_candidates.geom)
                    END AS geom
                FROM layer_a_boundary, forest_candidates
                LEFT JOIN difference_mask ON :use_difference_mask
                WHERE layer_a_boundary.geom IS NOT NULL
                  AND forest_candidates.geom IS NOT NULL
                  AND NOT ST_IsEmpty(layer_a_boundary.geom)
                  AND NOT ST_IsEmpty(forest_candidates.geom)
                  AND (
                      NOT :use_difference_mask
                      OR difference_mask.geom IS NOT NULL
                  )
            ),
            final_mask AS (
                SELECT
                    CASE
                        WHEN :clip_to_grid_version = '' THEN mask_base.geom
                        ELSE ST_Intersection(mask_base.geom, grid_clip.geom)
                    END AS geom
                FROM mask_base
                LEFT JOIN grid_clip ON :clip_to_grid_version <> ''
                WHERE :clip_to_grid_version = ''
                   OR grid_clip.geom IS NOT NULL
            ),
            clean_components AS (
                SELECT
                    ST_Multi(ST_CollectionExtract(ST_MakeValid(component.geom), 3)) AS geom
                FROM final_mask
                CROSS JOIN LATERAL ST_Dump(
                    ST_CollectionExtract(ST_MakeValid(final_mask.geom), 3)
                ) AS component
                WHERE final_mask.geom IS NOT NULL
                  AND NOT ST_IsEmpty(final_mask.geom)
                  AND (
                      :min_mask_component_area_ha <= 0
                      OR ST_Area(ST_Transform(component.geom, :projected_srid)) / 10000.0 >= :min_mask_component_area_ha
                  )
            ),
            prepared AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(ST_MakeValid(ST_UnaryUnion(ST_Collect(geom))), 3)
                )::geometry(MultiPolygon, 4326) AS geom
                FROM clean_components
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            changed AS (
                INSERT INTO regions (name, code, country_code, geom, metadata)
                SELECT :name, :code, :country_code, geom, CAST(:metadata AS jsonb)
                FROM prepared
                ON CONFLICT (code) DO UPDATE
                SET name = EXCLUDED.name,
                    country_code = EXCLUDED.country_code,
                    geom = EXCLUDED.geom,
                    metadata = EXCLUDED.metadata
                RETURNING code, name, country_code, geom, metadata
            )
            SELECT
                code,
                name,
                country_code,
                ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha,
                metadata->>'stage3_region_source' AS source
            FROM changed
            """
        ),
        {
            "forest_mask_source_layers": source_layers,
            "use_mescere_forest_class": _uses_mescere_forest_class(source_layers),
            "intersect_source_layers": intersect_source_layers,
            "use_intersect_mask": bool(intersect_source_layers),
            "difference_source_layers": difference_source_layers,
            "use_difference_mask": bool(difference_source_layers),
            "source_region_code": source_region_code,
            "clip_to_grid_version": clip_to_grid_version,
            "min_mask_component_area_ha": min_mask_component_area_ha,
            "name": region_name,
            "code": region_code,
            "country_code": country_code,
            "projected_srid": projected_srid,
            "metadata": json.dumps(metadata, separators=(",", ":")),
        },
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError(f"no forest mask geometries found for region {source_region_code}")
    return dict(row)


def _can_use_staged_source_union(
    *,
    source_layers: list[str],
    intersect_source_layers: list[str],
    difference_source_layers: list[str],
    clip_to_grid_version: str,
) -> bool:
    return (
        bool(source_layers)
        and not _uses_mescere_forest_class(source_layers)
        and not clip_to_grid_version
    )


def _upsert_region_from_staged_source_union(
    db,
    *,
    region_code: str,
    region_name: str,
    country_code: str,
    source_region_code: str,
    source_layers: list[str],
    intersect_source_layers: list[str],
    difference_source_layers: list[str],
    projected_srid: int,
    union_tile_size_m: int,
    min_mask_component_area_ha: float,
    use_source_mask_cache: bool,
    metadata: dict,
) -> dict:
    _create_tiled_coverage_stage(
        db,
        source_region_code=source_region_code,
        source_layers=source_layers,
        intersect_source_layers=intersect_source_layers,
        difference_source_layers=difference_source_layers,
        projected_srid=projected_srid,
        union_tile_size_m=union_tile_size_m,
        min_mask_component_area_ha=min_mask_component_area_ha,
        use_source_mask_cache=use_source_mask_cache,
    )
    count = _union_stage_count(db)
    if count == 0:
        raise RuntimeError(f"no forest mask geometries found for region {source_region_code}")

    row = None
    if use_source_mask_cache:
        row = _upsert_component_backed_region(
            db,
            region_code=region_code,
            region_name=region_name,
            country_code=country_code,
            projected_srid=projected_srid,
            min_mask_component_area_ha=min_mask_component_area_ha,
            metadata=metadata,
        )
    if row:
        return row

    row = db.execute(
        text(
            """
            WITH batch_union AS (
                SELECT ST_CollectionExtract(
                    ST_UnaryUnion(
                        ST_Collect(
                            ST_CollectionExtract(
                                ST_MakeValid(ST_SnapToGrid(geom, 0.01)),
                                3
                            )
                        ),
                        0.01
                    ),
                    3
                ) AS geom
                FROM stage3_prepare_region_union_work
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                GROUP BY floor((id - 1) / 100)
            ),
            raw_union AS (
                SELECT ST_CollectionExtract(
                    ST_UnaryUnion(
                        ST_Collect(
                            ST_CollectionExtract(
                                ST_MakeValid(ST_SnapToGrid(geom, 0.01)),
                                3
                            )
                        ),
                        0.01
                    ),
                    3
                ) AS geom
                FROM batch_union
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            clean_components AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(ST_MakeValid(component.geom), 3)
                ) AS geom
                FROM raw_union
                CROSS JOIN LATERAL ST_Dump(raw_union.geom) AS component
                WHERE raw_union.geom IS NOT NULL
                  AND NOT ST_IsEmpty(raw_union.geom)
                  AND (
                      :min_mask_component_area_ha <= 0
                      OR ST_Area(component.geom) / 10000.0 >= :min_mask_component_area_ha
                  )
            ),
            final_mask AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_Transform(ST_UnaryUnion(ST_Collect(geom)), 4326)),
                        3
                    )
                )::geometry(MultiPolygon, 4326) AS geom
                FROM clean_components
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            changed AS (
                INSERT INTO regions (name, code, country_code, geom, metadata)
                SELECT :name, :code, :country_code, geom, CAST(:metadata AS jsonb)
                FROM final_mask
                ON CONFLICT (code) DO UPDATE
                SET name = EXCLUDED.name,
                    country_code = EXCLUDED.country_code,
                    geom = EXCLUDED.geom,
                    metadata = EXCLUDED.metadata
                RETURNING code, name, country_code, geom, metadata
            )
            SELECT
                code,
                name,
                country_code,
                ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha,
                metadata->>'stage3_region_source' AS source
            FROM changed
            """
        ),
        {
            "name": region_name,
            "code": region_code,
            "country_code": country_code,
            "projected_srid": projected_srid,
            "min_mask_component_area_ha": min_mask_component_area_ha,
            "metadata": json.dumps(metadata, separators=(",", ":")),
        },
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError(f"no forest mask geometries found for region {source_region_code}")
    return dict(row)


def _upsert_component_backed_region(
    db,
    *,
    region_code: str,
    region_name: str,
    country_code: str,
    projected_srid: int,
    min_mask_component_area_ha: float,
    metadata: dict,
) -> dict | None:
    mask_version = str((metadata.get("forest_mask") or {}).get("mask_version") or region_code)
    db.execute(text("SET LOCAL work_mem = '128MB'"))
    db.execute(text("DROP TABLE IF EXISTS stage3_prepare_region_final_components"))
    db.execute(text("DROP TABLE IF EXISTS stage3_prepare_region_component_union_seed"))
    db.execute(text("ANALYZE stage3_prepare_region_union_work"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_prepare_region_component_union_seed ON COMMIT DROP AS
            SELECT
                row_number() OVER (ORDER BY source_id, ST_Area(geom) DESC)::integer AS id,
                geom
            FROM (
                SELECT
                    source.id AS source_id,
                    ST_CollectionExtract(part.geom, 3) AS geom
                FROM stage3_prepare_region_union_work source
                CROSS JOIN LATERAL ST_Subdivide(
                    ST_CollectionExtract(ST_MakeValid(ST_SnapToGrid(source.geom, 0.01)), 3),
                    :subdivide_max_vertices
                ) AS part(geom)
                WHERE source.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source.geom)
            ) seed
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            """
        ),
        {"subdivide_max_vertices": COMPONENT_SUBDIVIDE_MAX_VERTICES},
    )
    db.execute(text("ANALYZE stage3_prepare_region_component_union_seed"))
    union_source_table = _create_hierarchical_component_union(
        db,
        source_table="stage3_prepare_region_component_union_seed",
        target_prefix="stage3_prepare_region_component_union_l",
        batch_size=COMPONENT_UNION_BATCH_SIZE,
        final_target_rows=COMPONENT_UNION_FINAL_TARGET_ROWS,
        max_levels=COMPONENT_UNION_MAX_LEVELS,
    )
    db.execute(
        text(
            f"""
            CREATE TEMP TABLE stage3_prepare_region_final_components ON COMMIT DROP AS
            WITH raw_union AS (
                SELECT ST_CollectionExtract(
                    ST_MakeValid(geom),
                    3
                ) AS geom
                FROM {union_source_table}
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            components AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(ST_MakeValid(component.geom), 3)
                ) AS geom
                FROM raw_union
                CROSS JOIN LATERAL ST_Dump(raw_union.geom) AS component
                WHERE raw_union.geom IS NOT NULL
                  AND NOT ST_IsEmpty(raw_union.geom)
            )
            SELECT
                row_number() OVER (ORDER BY ST_Area(geom) DESC, ST_AsEWKB(ST_SnapToGrid(geom, 0.01))::text)::integer
                    AS component_index,
                geom,
                ST_Area(geom) / 10000.0 AS area_ha
            FROM components
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
              AND (
                  :min_mask_component_area_ha <= 0
                  OR ST_Area(geom) / 10000.0 >= :min_mask_component_area_ha
              )
            """
        ),
        {"min_mask_component_area_ha": min_mask_component_area_ha},
    )
    db.execute(text("CREATE INDEX stage3_prepare_region_final_components_gix ON stage3_prepare_region_final_components USING GIST (geom)"))
    db.execute(text("ANALYZE stage3_prepare_region_final_components"))
    row = db.execute(
        text(
            """
            WITH final_mask AS (
                SELECT
                    ST_Multi(ST_CollectionExtract(ST_Collect(geom), 3)) AS geom,
                    count(*)::bigint AS component_count,
                    COALESCE(sum(ST_Area(geom) / 10000.0), 0.0) AS area_ha
                FROM stage3_prepare_region_final_components
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            changed AS (
                INSERT INTO regions (name, code, country_code, geom, metadata)
                SELECT
                    :name,
                    :code,
                    :country_code,
                    ST_Multi(ST_Transform(geom, 4326))::geometry(MultiPolygon, 4326),
                    CAST(:metadata AS jsonb)
                FROM final_mask
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                ON CONFLICT (code) DO UPDATE
                SET name = EXCLUDED.name,
                    country_code = EXCLUDED.country_code,
                    geom = EXCLUDED.geom,
                    metadata = EXCLUDED.metadata
                RETURNING id, code, name, country_code, metadata
            )
            SELECT
                changed.id,
                changed.code,
                changed.name,
                changed.country_code,
                (SELECT area_ha FROM final_mask) AS area_ha,
                changed.metadata->>'stage3_region_source' AS source
            FROM changed
            """
        ),
        {
            "name": region_name,
            "code": region_code,
            "country_code": country_code,
            "projected_srid": projected_srid,
            "metadata": json.dumps(metadata, separators=(",", ":")),
        },
    ).mappings().one_or_none()
    if not row:
        return None

    region_id = row.get("id") if hasattr(row, "get") else row["id"]
    if not region_id:
        return dict(row)

    db.execute(
        text(
            """
            DELETE FROM stage3_region_mask_components
            WHERE region_id = :region_id
              AND mask_version = :mask_version
              AND projected_srid = :projected_srid
            """
        ),
        {"region_id": region_id, "mask_version": mask_version, "projected_srid": projected_srid},
    )
    db.execute(
        text(
            """
            INSERT INTO stage3_region_mask_components (
                region_id,
                mask_version,
                component_index,
                projected_srid,
                geom_4326,
                geom_projected,
                area_ha,
                metadata
            )
            SELECT
                :region_id,
                :mask_version,
                component_index,
                :projected_srid,
                ST_Multi(ST_Transform(geom, 4326)),
                ST_Multi(geom),
                area_ha,
                jsonb_build_object('pipeline', 'stage3_prepare_region_component_mask')
            FROM stage3_prepare_region_final_components
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            """
        ),
        {"region_id": region_id, "mask_version": mask_version, "projected_srid": projected_srid},
    )
    return dict(row)


def _create_hierarchical_component_union(
    db,
    *,
    source_table: str,
    target_prefix: str,
    batch_size: int,
    final_target_rows: int,
    max_levels: int,
) -> str:
    current_table = source_table
    current_count = _temp_table_count(db, current_table)
    for level in range(1, max_levels + 1):
        if current_count <= final_target_rows:
            return current_table
        next_table = f"{target_prefix}{level}"
        db.execute(text(f"DROP TABLE IF EXISTS {next_table}"))
        db.execute(
            text(
                f"""
                CREATE TEMP TABLE {next_table} ON COMMIT DROP AS
                SELECT
                    row_number() OVER (ORDER BY batch_id)::integer AS id,
                    geom
                FROM (
                    SELECT
                        floor((id - 1) / :component_union_batch_size)::bigint AS batch_id,
                        ST_CollectionExtract(
                            ST_UnaryUnion(
                                ST_Collect(geom),
                                0.01
                            ),
                            3
                        ) AS geom
                    FROM {current_table}
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                    GROUP BY floor((id - 1) / :component_union_batch_size)
                ) batch_union
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                """
            ),
            {"component_union_batch_size": batch_size},
        )
        db.execute(text(f"ANALYZE {next_table}"))
        current_table = next_table
        current_count = _temp_table_count(db, current_table)
    raise RuntimeError(
        f"component union did not converge below {final_target_rows} rows after {max_levels} levels"
    )


def _temp_table_count(db, table_name: str) -> int:
    return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _create_tiled_coverage_stage(
    db,
    *,
    source_region_code: str,
    source_layers: list[str],
    intersect_source_layers: list[str],
    difference_source_layers: list[str],
    projected_srid: int,
    union_tile_size_m: int = TILED_COVERAGE_UNION_TILE_SIZE_M,
    min_mask_component_area_ha: float = 0,
    use_source_mask_cache: bool = False,
) -> None:
    db.execute(text("DROP TABLE IF EXISTS stage3_prepare_region_source_geoms"))
    _create_source_stage(
        db,
        table_name="stage3_prepare_region_source_geoms",
        source_region_code=source_region_code,
        source_layers=source_layers,
        projected_srid=projected_srid,
        use_source_mask_cache=use_source_mask_cache,
    )
    _create_auxiliary_stage(
        db,
        table_name="stage3_prepare_region_intersect_geoms",
        source_region_code=source_region_code,
        source_layers=intersect_source_layers,
        projected_srid=projected_srid,
        use_source_mask_cache=use_source_mask_cache,
    )
    _create_auxiliary_stage(
        db,
        table_name="stage3_prepare_region_difference_geoms",
        source_region_code=source_region_code,
        source_layers=difference_source_layers,
        projected_srid=projected_srid,
        use_source_mask_cache=use_source_mask_cache,
    )
    _create_prepare_region_tiles(
        db,
        projected_srid=projected_srid,
        union_tile_size_m=union_tile_size_m,
    )
    _create_prepare_region_dissolved_stage(
        db,
        source_table="stage3_prepare_region_source_geoms",
        target_table="stage3_prepare_region_source_dissolved",
        projected_srid=projected_srid,
        enabled=True,
    )
    _create_prepare_region_dissolved_stage(
        db,
        source_table="stage3_prepare_region_intersect_geoms",
        target_table="stage3_prepare_region_intersect_dissolved",
        projected_srid=projected_srid,
        enabled=bool(intersect_source_layers),
    )
    _create_prepare_region_dissolved_stage(
        db,
        source_table="stage3_prepare_region_difference_geoms",
        target_table="stage3_prepare_region_difference_dissolved",
        projected_srid=projected_srid,
        enabled=bool(difference_source_layers),
    )
    _create_prepare_region_union_work(
        db,
        use_intersect_mask=bool(intersect_source_layers),
        use_difference_mask=bool(difference_source_layers),
        min_mask_component_area_ha=min_mask_component_area_ha,
    )


def _create_prepare_region_tiles(db, *, projected_srid: int, union_tile_size_m: int) -> None:
    db.execute(text("DROP TABLE IF EXISTS stage3_prepare_region_tiles"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_prepare_region_tiles ON COMMIT DROP AS
            WITH projected_extent AS (
                SELECT
                    floor(ST_XMin(geom) / :tile_size_m)::integer AS min_i,
                    floor(ST_YMin(geom) / :tile_size_m)::integer AS min_j,
                    ceil(ST_XMax(geom) / :tile_size_m)::integer AS max_i,
                    ceil(ST_YMax(geom) / :tile_size_m)::integer AS max_j
                FROM (
                    SELECT ST_SetSRID(ST_Extent(geom)::geometry, :projected_srid) AS geom
                    FROM stage3_prepare_region_source_geoms
                ) extent
                WHERE geom IS NOT NULL
            ),
            tiles AS (
            SELECT
                i,
                j,
                ST_MakeEnvelope(
                        i * :tile_size_m,
                        j * :tile_size_m,
                        (i + 1) * :tile_size_m,
                        (j + 1) * :tile_size_m,
                        :projected_srid
                    ) AS geom
                FROM projected_extent,
                     generate_series(min_i, max_i - 1) AS i,
                     generate_series(min_j, max_j - 1) AS j
            )
            SELECT i, j, geom
            FROM tiles
            """
        ),
        {"projected_srid": projected_srid, "tile_size_m": union_tile_size_m},
    )
    db.execute(text("CREATE INDEX stage3_prepare_region_tiles_key_idx ON stage3_prepare_region_tiles (i, j)"))
    db.execute(text("CREATE INDEX stage3_prepare_region_tiles_gix ON stage3_prepare_region_tiles USING GIST (geom)"))
    db.execute(text("ANALYZE stage3_prepare_region_tiles"))


def _create_prepare_region_dissolved_stage(
    db,
    *,
    source_table: str,
    target_table: str,
    projected_srid: int,
    enabled: bool,
) -> None:
    db.execute(text(f"DROP TABLE IF EXISTS {target_table}"))
    if not enabled:
        db.execute(
            text(
                f"""
                CREATE TEMP TABLE {target_table} ON COMMIT DROP AS
                SELECT
                    NULL::integer AS i,
                    NULL::integer AS j,
                    NULL::geometry AS geom
                WHERE false
                """
            )
        )
        return
    db.execute(
        text(
            f"""
            CREATE TEMP TABLE {target_table} ON COMMIT DROP AS
            WITH clipped AS (
                SELECT
                    tile.i,
                    tile.j,
                    ST_CollectionExtract(
                        ST_MakeValid(ST_Intersection(source.geom, tile.geom)),
                        3
                    ) AS geom
                FROM stage3_prepare_region_tiles tile
                JOIN {source_table} source
                  ON source.geom && tile.geom
                 AND ST_Intersects(source.geom, tile.geom)
            )
            SELECT
                i,
                j,
                ST_Multi(
                    ST_CollectionExtract(ST_UnaryUnion(ST_Collect(geom)), 3)
                ) AS geom
            FROM clipped
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            GROUP BY i, j
            """
        )
    )
    db.execute(text(f"CREATE INDEX {target_table}_key_idx ON {target_table} (i, j)"))
    db.execute(text(f"ANALYZE {target_table}"))


def _create_prepare_region_union_work(
    db,
    *,
    use_intersect_mask: bool,
    use_difference_mask: bool,
    min_mask_component_area_ha: float = 0,
) -> None:
    db.execute(text("DROP TABLE IF EXISTS stage3_prepare_region_union_work"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_prepare_region_union_work ON COMMIT DROP AS
            WITH
            refined AS (
                SELECT
                    source.i,
                    source.j,
                    CASE
                        WHEN :use_difference_mask AND difference_mask.geom IS NOT NULL
                        THEN ST_MakeValid(ST_Difference(ST_MakeValid(base.geom), difference_mask.geom))
                        ELSE base.geom
                    END AS geom
                FROM stage3_prepare_region_source_dissolved source
                LEFT JOIN stage3_prepare_region_intersect_dissolved intersect_mask
                  ON intersect_mask.i = source.i AND intersect_mask.j = source.j
                LEFT JOIN stage3_prepare_region_difference_dissolved difference_mask
                  ON difference_mask.i = source.i AND difference_mask.j = source.j
                CROSS JOIN LATERAL (
                    SELECT CASE
                        WHEN :use_intersect_mask AND intersect_mask.geom IS NOT NULL
                        THEN ST_Intersection(source.geom, intersect_mask.geom)
                        WHEN :use_intersect_mask
                        THEN NULL
                        ELSE source.geom
                    END AS geom
                ) base
                WHERE base.geom IS NOT NULL
                  AND NOT ST_IsEmpty(base.geom)
            )
            SELECT
                row_number() OVER (ORDER BY i, j) AS id,
                ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)) AS geom
            FROM refined
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            """
        ),
        {
            "use_intersect_mask": use_intersect_mask,
            "use_difference_mask": use_difference_mask,
        },
    )


def _create_auxiliary_stage(
    db,
    *,
    table_name: str,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
    use_source_mask_cache: bool = False,
) -> None:
    _create_source_stage(
        db,
        table_name=table_name,
        source_region_code=source_region_code,
        source_layers=source_layers,
        projected_srid=projected_srid,
        use_source_mask_cache=use_source_mask_cache,
    )


def _create_source_stage(
    db,
    *,
    table_name: str,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
    use_source_mask_cache: bool = False,
) -> None:
    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    if use_source_mask_cache:
        db.execute(
            text(
                f"""
                CREATE TEMP TABLE {table_name} ON COMMIT DROP AS
                WITH source_region AS (
                    SELECT ST_Transform(ST_MakeValid(geom), :projected_srid) AS geom
                    FROM regions
                    WHERE code = :source_region_code
                )
                SELECT ST_MakeValid(ST_Intersection(cache.geom_projected, source_region.geom)) AS geom
                FROM stage3_source_mask_cache cache
                JOIN source_region
                  ON source_region.geom IS NOT NULL
                 AND NOT ST_IsEmpty(source_region.geom)
                 AND cache.geom_projected && source_region.geom
                 AND ST_Intersects(cache.geom_projected, source_region.geom)
                WHERE cache.source_region_code = :source_region_code
                  AND cache.projected_srid = :projected_srid
                  AND cache.source_layer = ANY(:source_layers)
                  AND cache.geom_projected IS NOT NULL
                  AND NOT ST_IsEmpty(cache.geom_projected)
                """
            ),
            {
                "source_layers": source_layers,
                "source_region_code": source_region_code,
                "projected_srid": projected_srid,
            },
        )
    else:
        db.execute(
            text(
                f"""
                CREATE TEMP TABLE {table_name} ON COMMIT DROP AS
                WITH source_region AS (
                    SELECT ST_MakeValid(geom) AS geom
                    FROM regions
                    WHERE code = :source_region_code
                )
                SELECT ST_Transform(
                    ST_MakeValid(ST_Intersection(ST_MakeValid(parcel.geom), source_region.geom)),
                    :projected_srid
                ) AS geom
                FROM ogm_parcels parcel
                JOIN source_region
                  ON source_region.geom IS NOT NULL
                 AND NOT ST_IsEmpty(source_region.geom)
                 AND parcel.geom && source_region.geom
                 AND ST_Intersects(ST_MakeValid(parcel.geom), source_region.geom)
                WHERE parcel.is_current
                  AND parcel.source_layer = ANY(:source_layers)
                  AND parcel.attributes->>'region_code' = :source_region_code
                  AND parcel.geom IS NOT NULL
                  AND NOT ST_IsEmpty(parcel.geom)
                """
            ),
            {
                "source_layers": source_layers,
                "source_region_code": source_region_code,
                "projected_srid": projected_srid,
            },
        )
    db.execute(text(f"CREATE INDEX {table_name}_gix ON {table_name} USING GIST (geom)"))
    db.execute(text(f"ANALYZE {table_name}"))


def _union_stage_count(db) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM stage3_prepare_region_union_work
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                """
            )
        ).scalar_one()
    )


def _uses_mescere_forest_class(source_layers: list[str]) -> bool:
    return SOURCE_LAYER_MESCERE in source_layers


def _validate_area(area_ha: float, min_area_ha: float, max_area_ha: float) -> None:
    if min_area_ha and area_ha < min_area_ha:
        raise RuntimeError(f"region area {round(area_ha, 2)} ha is below required minimum {min_area_ha} ha")
    if max_area_ha and area_ha > max_area_ha:
        raise RuntimeError(f"region area {round(area_ha, 2)} ha is above required maximum {max_area_ha} ha")


if __name__ == "__main__":
    main()
