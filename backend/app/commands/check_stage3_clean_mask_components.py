from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal


DEFAULT_PROJECTED_SRID = 6933


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check that the final Stage 3 clean forest mask has no tiny post-difference components"
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", default="")
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--min-component-area-ha", type=float, default=1.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.min_component_area_ha < 0:
        raise ValueError("min-component-area-ha must not be negative")

    with SessionLocal() as db:
        report = clean_mask_component_report(
            db,
            region_code=args.region_code,
            grid_version=args.grid_version,
            projected_srid=args.projected_srid,
            min_component_area_ha=args.min_component_area_ha,
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["checks"]["ready"]:
        raise SystemExit(1)


def clean_mask_component_report(
    db,
    *,
    region_code: str,
    grid_version: str = "",
    projected_srid: int = DEFAULT_PROJECTED_SRID,
    min_component_area_ha: float = 1.0,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH region AS (
                SELECT
                    id,
                    metadata,
                    ST_Transform(geom, :projected_srid) AS geom
                FROM regions
                WHERE code = :region_code
            ),
            component_table AS (
                SELECT component.geom_projected AS geom
                FROM region
                JOIN stage3_region_mask_components component
                  ON component.region_id = region.id
                 AND component.projected_srid = :projected_srid
                 AND component.mask_version = COALESCE(
                        region.metadata->'forest_mask'->>'mask_version',
                        :region_code
                     )
            ),
            region_components AS (
                SELECT ST_Multi(ST_CollectionExtract(component.geom, 3)) AS geom
                FROM region
                CROSS JOIN LATERAL ST_Dump(
                    ST_CollectionExtract(region.geom, 3)
                ) AS component
                WHERE region.geom IS NOT NULL
                  AND NOT ST_IsEmpty(region.geom)
                  AND NOT EXISTS (SELECT 1 FROM component_table)
            ),
            components AS (
                SELECT geom FROM component_table
                UNION ALL
                SELECT geom FROM region_components
            ),
            component_metrics AS (
                SELECT
                    count(*)::bigint AS component_count,
                    count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom))::bigint AS empty_components,
                    count(*) FILTER (WHERE NOT ST_IsValid(geom))::bigint AS invalid_components,
                    count(*) FILTER (
                        WHERE ST_Area(geom) / 10000.0 < :min_component_area_ha
                    )::bigint AS components_below_min_area,
                    COALESCE(min(ST_Area(geom) / 10000.0), 0)::float AS min_component_area_ha,
                    COALESCE(max(ST_Area(geom) / 10000.0), 0)::float AS max_component_area_ha,
                    COALESCE(sum(ST_Area(geom) / 10000.0), 0)::float AS total_component_area_ha,
                    COALESCE(sum(ST_Area(geom) / 10000.0) FILTER (
                        WHERE ST_Area(geom) / 10000.0 < :min_component_area_ha
                    ), 0)::float AS area_below_min_ha
                FROM components
            ),
            grid_metrics AS (
                SELECT
                    count(cell.id)::bigint AS cell_count,
                    count(cell.id) FILTER (WHERE cell.area_ha < 0.03)::bigint AS cells_below_0_03ha,
                    count(cell.id) FILTER (WHERE cell.area_ha < 0.17)::bigint AS cells_below_0_17ha,
                    count(cell.id) FILTER (WHERE cell.area_ha < 0.5)::bigint AS cells_below_0_5ha,
                    COALESCE(min(cell.area_ha), 0)::float AS min_cell_area_ha,
                    COALESCE(max(cell.area_ha), 0)::float AS max_cell_area_ha
                FROM region
                LEFT JOIN grid_cells cell
                  ON :grid_version <> ''
                 AND cell.region_id = region.id
                 AND cell.grid_version = :grid_version
            )
            SELECT
                region.metadata AS metadata,
                component_metrics.*,
                grid_metrics.*
            FROM region, component_metrics, grid_metrics
            """
        ),
        {
            "region_code": region_code,
            "grid_version": grid_version,
            "projected_srid": projected_srid,
            "min_component_area_ha": min_component_area_ha,
        },
    ).mappings().one()
    metadata = _json_dict(row["metadata"])
    clean_policy = ((metadata.get("forest_mask") or {}).get("clean_mask_policy") or {})
    checks = _component_checks(row, clean_policy=clean_policy, min_component_area_ha=min_component_area_ha)
    return {
        "region_code": region_code,
        "grid_version": grid_version,
        "projected_srid": projected_srid,
        "min_component_area_ha": float(min_component_area_ha),
        "component_count": int(row["component_count"] or 0),
        "invalid_components": int(row["invalid_components"] or 0),
        "empty_components": int(row["empty_components"] or 0),
        "components_below_min_area": int(row["components_below_min_area"] or 0),
        "min_component_area_ha_observed": round(float(row["min_component_area_ha"] or 0), 6),
        "max_component_area_ha_observed": round(float(row["max_component_area_ha"] or 0), 6),
        "total_component_area_ha": round(float(row["total_component_area_ha"] or 0), 6),
        "area_below_min_ha": round(float(row["area_below_min_ha"] or 0), 6),
        "grid_area_buckets": {
            "cell_count": int(row["cell_count"] or 0),
            "cells_below_0_03ha": int(row["cells_below_0_03ha"] or 0),
            "cells_below_0_17ha": int(row["cells_below_0_17ha"] or 0),
            "cells_below_0_5ha": int(row["cells_below_0_5ha"] or 0),
            "min_cell_area_ha": round(float(row["min_cell_area_ha"] or 0), 6),
            "max_cell_area_ha": round(float(row["max_cell_area_ha"] or 0), 6),
        },
        "clean_mask_policy": clean_policy,
        "checks": checks,
    }


def _component_checks(row, *, clean_policy: dict[str, Any], min_component_area_ha: float) -> dict[str, bool]:
    policy_min = _float(clean_policy.get("min_component_area_ha"))
    checks = {
        "has_components": int(row["component_count"] or 0) > 0,
        "valid_components": int(row["invalid_components"] or 0) == 0,
        "no_empty_components": int(row["empty_components"] or 0) == 0,
        "no_components_below_min_area": int(row["components_below_min_area"] or 0) == 0,
        "clean_policy_recorded": clean_policy.get("method") == "component_area_filter_before_grid_generation",
        "clean_policy_applies_before_grid_insert": bool(clean_policy.get("applies_before_grid_insert")),
        "clean_policy_threshold_matches": policy_min >= float(min_component_area_ha),
    }
    checks["ready"] = all(checks.values())
    return checks


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return {}


if __name__ == "__main__":
    main()
