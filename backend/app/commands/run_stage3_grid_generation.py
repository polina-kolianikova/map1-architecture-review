from __future__ import annotations

import argparse
import math

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.forest_scope import require_forest_only_region
from app.services.grid import TEST_PROJECTED_SRID, generate_region_grid
from app.services.stage3_grid_options import projected_srid_for_lon_lat
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_PROJECTED_SRID,
    is_stage3_public_v9_full_scope,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a stable grid for a configured region")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--grid-version", required=True)
    parser.add_argument("--grid-size-m", type=int, default=100)
    parser.add_argument("--projected-srid", type=int, default=TEST_PROJECTED_SRID)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--country-code", default="TR")
    parser.add_argument("--region-wkt-file", default="")
    parser.add_argument("--max-direct-cells", type=int, default=100000)
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.region_wkt_file:
            raise RuntimeError(
                "region-wkt-file is handled by app.commands.run_stage3_prepare_region; "
                "prepare the forest-only region first, then run grid generation"
            )
        _require_legacy_direct_grid_not_full(
            region_code=args.region_code,
            grid_version=args.grid_version,
        )
        require_forest_only_region(
            db,
            region_code=args.region_code,
        )
        if args.projected_srid == 0:
            args.projected_srid = _auto_projected_srid(db, args.region_code)
        scope_area_ha = _region_area_ha(db, args.region_code, STAGE3_PUBLIC_V9_PROJECTED_SRID)
        _require_legacy_direct_grid_not_full(
            region_code=args.region_code,
            grid_version=args.grid_version,
            scope_area_ha=scope_area_ha,
        )
        estimated_cells = _estimated_cells_from_area(scope_area_ha, args.grid_size_m)
        if estimated_cells > args.max_direct_cells:
            raise RuntimeError(
                "region is too large for direct grid generation; "
                "use app.commands.run_stage3_grid_generation_batched"
            )
        result = generate_region_grid(
            db,
            region_code=args.region_code,
            grid_version=args.grid_version,
            grid_size_m=args.grid_size_m,
            projected_srid=args.projected_srid,
        )
        db.commit()

    print(
        "stage3 grid ready "
        f"region_code={result['region_code']} "
        f"grid_version={result['grid_version']} "
        f"cell_count={result['cell_count']}"
    )

def _auto_projected_srid(db, region_code: str) -> int:
    row = db.execute(
        text(
            """
            SELECT
                ST_X(ST_PointOnSurface(geom)) AS lon,
                ST_Y(ST_PointOnSurface(geom)) AS lat
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).mappings().one()
    return projected_srid_for_lon_lat(float(row["lon"]), float(row["lat"]))


def _estimated_cells(db, region_code: str, grid_size_m: int, projected_srid: int) -> int:
    return _estimated_cells_from_area(_region_area_ha(db, region_code, projected_srid), grid_size_m)


def _region_area_ha(db, region_code: str, projected_srid: int) -> float:
    area_ha = db.execute(
        text(
            """
            SELECT ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code, "projected_srid": projected_srid},
    ).scalar_one()
    return float(area_ha)


def _estimated_cells_from_area(area_ha: float, grid_size_m: int) -> int:
    cell_area_ha = (grid_size_m * grid_size_m) / 10000.0
    return int(math.ceil(float(area_ha) / cell_area_ha))


def _require_legacy_direct_grid_not_full(
    *,
    region_code: str,
    grid_version: str,
    scope_area_ha: float | None = None,
) -> None:
    if not is_stage3_public_v9_full_scope(
        region_code=region_code,
        grid_version=grid_version,
        scope_area_ha=scope_area_ha,
    ):
        return
    raise RuntimeError(
        "Legacy Stage 3 direct grid generation is disabled for full/national Turkey V10 scope; "
        "use app.commands.run_stage3_grid_generation_batched with --use-region-mask-components "
        "after explicit user approval."
    )


if __name__ == "__main__":
    main()
