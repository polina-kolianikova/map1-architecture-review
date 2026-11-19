from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_PROJECTED_SRID,
    is_stage3_public_v9_full_scope,
)


DEFAULT_PROJECTED_SRID = 6933


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a Stage 3 clean region from already-final mask components without a national union"
    )
    parser.add_argument("--source-region-code", required=True)
    parser.add_argument("--target-region-code", required=True)
    parser.add_argument("--target-region-name", default="")
    parser.add_argument("--country-code", default="TR")
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--min-component-area-ha", type=float, default=1.0)
    parser.add_argument("--mask-version", default="")
    args = parser.parse_args()
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.min_component_area_ha < 0:
        raise ValueError("min-component-area-ha must not be negative")

    with SessionLocal() as db:
        result = filter_region_components(
            db,
            source_region_code=args.source_region_code,
            target_region_code=args.target_region_code,
            target_region_name=args.target_region_name or args.target_region_code,
            country_code=args.country_code,
            projected_srid=args.projected_srid,
            min_component_area_ha=args.min_component_area_ha,
            mask_version=args.mask_version or args.target_region_code,
        )
        db.commit()

    print(
        "stage3 filtered region ready "
        f"source_region_code={args.source_region_code} "
        f"target_region_code={result['code']} "
        f"components_kept={result['components_kept']} "
        f"components_removed={result['components_removed']} "
        f"area_ha={round(result['area_ha'], 2)}"
    )


def filter_region_components(
    db,
    *,
    source_region_code: str,
    target_region_code: str,
    target_region_name: str,
    country_code: str = "TR",
    projected_srid: int = DEFAULT_PROJECTED_SRID,
    min_component_area_ha: float = 1.0,
    mask_version: str = "",
) -> dict[str, Any]:
    mask_version = mask_version or target_region_code
    _guard_legacy_filter_not_full(
        db,
        source_region_code=source_region_code,
        target_region_code=target_region_code,
        mask_version=mask_version,
    )
    metadata = _target_metadata(
        db,
        source_region_code=source_region_code,
        target_region_code=target_region_code,
        min_component_area_ha=min_component_area_ha,
    )
    row = db.execute(
        text(
            """
            WITH source_region AS (
                SELECT
                    geom,
                    ST_Transform(geom, :projected_srid) AS projected_geom
                FROM regions
                WHERE code = :source_region_code
            ),
            components AS (
                SELECT
                    row_number() OVER () AS component_index,
                    ST_Multi(ST_CollectionExtract(component.geom, 3)) AS geom_projected
                FROM source_region
                CROSS JOIN LATERAL ST_Dump(
                    ST_CollectionExtract(projected_geom, 3)
                ) AS component
                WHERE projected_geom IS NOT NULL
                  AND NOT ST_IsEmpty(projected_geom)
            ),
            measured AS (
                SELECT
                    component_index,
                    geom_projected,
                    ST_Area(geom_projected) / 10000.0 AS area_ha,
                    ST_IsValid(geom_projected) AS is_valid
                FROM components
                WHERE geom_projected IS NOT NULL
                  AND NOT ST_IsEmpty(geom_projected)
            ),
            accepted AS (
                SELECT geom_projected
                FROM measured
                WHERE is_valid
                  AND area_ha >= :min_component_area_ha
            ),
            valid_4326_components AS (
                SELECT ST_Multi(ST_CollectionExtract(component.geom, 3)) AS geom
                FROM accepted
                CROSS JOIN LATERAL ST_Dump(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_Transform(geom_projected, 4326)),
                        3
                    )
                ) AS component
            ),
            final_components AS (
                SELECT geom
                FROM valid_4326_components
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                  AND ST_IsValid(ST_Transform(geom, :projected_srid))
                  AND ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 >= :min_component_area_ha
            ),
            numbered_final_components AS (
                SELECT
                    row_number() OVER (ORDER BY ST_Area(ST_Transform(geom, :projected_srid)) DESC) AS component_index,
                    geom AS geom_4326,
                    ST_Transform(geom, :projected_srid) AS geom_projected
                FROM final_components
            ),
            prepared AS (
                SELECT ST_Multi(
                    ST_CollectionExtract(
                        ST_Collect(geom_4326),
                        3
                    )
                )::geometry(MultiPolygon, 4326) AS geom
                FROM numbered_final_components
            ),
            changed AS (
                INSERT INTO regions (name, code, country_code, geom, metadata)
                SELECT
                    :target_region_name,
                    :target_region_code,
                    :country_code,
                    geom,
                    CAST(:metadata AS jsonb)
                FROM prepared
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                ON CONFLICT (code) DO UPDATE
                SET name = EXCLUDED.name,
                    country_code = EXCLUDED.country_code,
                    geom = EXCLUDED.geom,
                    metadata = EXCLUDED.metadata
                RETURNING code, name, country_code, geom, metadata
            ),
            removed_components AS (
                DELETE FROM stage3_region_mask_components component
                USING changed
                WHERE component.region_id = (
                    SELECT id FROM regions WHERE code = changed.code
                )
                  AND component.mask_version = :mask_version
                RETURNING 1
            ),
            inserted_components AS (
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
                    region.id,
                    :mask_version,
                    component.component_index,
                    :projected_srid,
                    component.geom_4326,
                    component.geom_projected,
                    round((ST_Area(component.geom_projected) / 10000.0)::numeric, 4),
                    jsonb_build_object(
                        'source_region_code', :source_region_code,
                        'target_region_code', :target_region_code,
                        'mask_version', :mask_version,
                        'min_component_area_ha', :min_component_area_ha,
                        'source_geometry', 'final_post_difference_region_components'
                    )
                FROM numbered_final_components component
                JOIN regions region ON region.code = :target_region_code
                ON CONFLICT (region_id, mask_version, component_index) DO UPDATE
                SET projected_srid = EXCLUDED.projected_srid,
                    geom_4326 = EXCLUDED.geom_4326,
                    geom_projected = EXCLUDED.geom_projected,
                    area_ha = EXCLUDED.area_ha,
                    metadata = EXCLUDED.metadata
                RETURNING 1
            )
            SELECT
                changed.code,
                changed.name,
                changed.country_code,
                ST_Area(ST_Transform(changed.geom, :projected_srid)) / 10000.0 AS area_ha,
                (SELECT count(*) FROM inserted_components)::bigint AS components_kept,
                (SELECT count(*) FROM measured WHERE NOT is_valid OR area_ha < :min_component_area_ha)::bigint
                    AS components_removed,
                (SELECT count(*) FROM measured WHERE NOT is_valid)::bigint AS invalid_components_removed,
                (SELECT count(*) FROM measured WHERE area_ha < :min_component_area_ha)::bigint
                    AS small_components_removed
            FROM changed
            """
        ),
        {
            "source_region_code": source_region_code,
            "target_region_code": target_region_code,
            "target_region_name": target_region_name,
            "country_code": country_code,
            "projected_srid": projected_srid,
            "min_component_area_ha": min_component_area_ha,
            "mask_version": mask_version,
            "metadata": json.dumps(metadata, separators=(",", ":")),
        },
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError(f"no accepted clean mask components found for region {source_region_code}")
    return dict(row)


def _guard_legacy_filter_not_full(
    db,
    *,
    source_region_code: str,
    target_region_code: str,
    mask_version: str,
) -> None:
    if _is_legacy_filter_full_scope(
        source_region_code=source_region_code,
        target_region_code=target_region_code,
        mask_version=mask_version,
    ):
        _raise_legacy_filter_full_scope()
    if _is_legacy_filter_full_scope(
        source_region_code=source_region_code,
        target_region_code=target_region_code,
        mask_version=mask_version,
        scope_area_ha=_existing_region_area_ha(db, source_region_code),
    ):
        _raise_legacy_filter_full_scope()


def _is_legacy_filter_full_scope(
    *,
    source_region_code: str,
    target_region_code: str,
    mask_version: str,
    scope_area_ha: float | None = None,
) -> bool:
    return is_stage3_public_v9_full_scope(
        region_code=target_region_code,
        grid_version=mask_version,
        source_region_code=source_region_code,
        clip_region_code=source_region_code,
        scope_area_ha=scope_area_ha,
    ) or is_stage3_public_v9_full_scope(
        region_code=source_region_code,
        grid_version=mask_version,
        scope_area_ha=scope_area_ha,
    )


def _raise_legacy_filter_full_scope() -> None:
    raise RuntimeError(
        "Legacy Stage 3 component filtering is disabled for full/national Turkey V10 scope; "
        "use app.commands.run_stage3_prepare_region_fragments so the persisted components "
        "are produced by the exact client formula before grid generation."
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


def _target_metadata(
    db,
    *,
    source_region_code: str,
    target_region_code: str,
    min_component_area_ha: float,
) -> dict[str, Any]:
    source_metadata = db.execute(
        text("SELECT metadata FROM regions WHERE code = :source_region_code"),
        {"source_region_code": source_region_code},
    ).scalar_one()
    metadata = _json_dict(source_metadata)
    forest_mask = metadata.setdefault("forest_mask", {})
    forest_mask["filtered_from_region_code"] = source_region_code
    forest_mask["filtered_target_region_code"] = target_region_code
    forest_mask["mask_components_table"] = "stage3_region_mask_components"
    forest_mask["mask_version"] = target_region_code
    forest_mask["clean_mask_policy"] = {
        "method": "component_area_filter_before_grid_generation",
        "min_component_area_ha": float(min_component_area_ha),
        "applies_before_grid_insert": True,
        "source_geometry": "final_post_difference_region_components",
        "client_decision": "exclude_tiny_nonforest_or_fragmented_scraps_before_stage3_acceptance",
    }
    forest_mask["method"] = f"{forest_mask.get('method') or 'forest_mask_geometry'}_final_component_filter"
    metadata["forest_only"] = True
    metadata["stage3_region_source"] = "filtered_final_forest_mask_components"
    metadata["future_full_layer_a_source_adapter"] = True
    return metadata


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return {}


if __name__ == "__main__":
    main()
