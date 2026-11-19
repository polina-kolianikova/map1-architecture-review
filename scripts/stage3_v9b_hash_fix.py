import os

from sqlalchemy import text

from app.db.session import SessionLocal


REGION = "TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10"
GRID = "stage3-public-ogm-osm-union-clean-v10"


def main() -> None:
    if os.environ.get("MAP1_FULL_TURKEY_APPROVED") != "YES":
        raise SystemExit(
            "Full Turkey metadata mutation blocked. Set MAP1_FULL_TURKEY_APPROVED=YES only after explicit user approval."
        )
    with SessionLocal() as db:
        source_hash = db.execute(
            text(
                """
                WITH scope AS (
                    SELECT id
                    FROM regions
                    WHERE code = :region
                )
                SELECT md5(
                    concat_ws(
                        ':',
                        count(*)::text,
                        COALESCE(sum(component.area_ha), 0)::text,
                        COALESCE(min(component.area_ha), 0)::text,
                        COALESCE(max(component.area_ha), 0)::text,
                        COALESCE(ST_XMin(ST_Extent(component.geom_projected))::text, ''),
                        COALESCE(ST_YMin(ST_Extent(component.geom_projected))::text, ''),
                        COALESCE(ST_XMax(ST_Extent(component.geom_projected))::text, ''),
                        COALESCE(ST_YMax(ST_Extent(component.geom_projected))::text, '')
                    )
                ) AS source_hash
                FROM stage3_region_mask_components component, scope
                WHERE component.region_id = scope.id
                  AND component.mask_version = :region
                  AND component.projected_srid = 6933
                """
            ),
            {"region": REGION},
        ).scalar_one()
        db.execute(
            text(
                """
                WITH scope AS (
                    SELECT id
                    FROM regions
                    WHERE code = :region
                ),
                latest AS (
                    SELECT run.id
                    FROM stage3_grid_generation_runs run, scope
                    WHERE run.region_id = scope.id
                      AND run.grid_version = :grid
                    ORDER BY run.id DESC
                    LIMIT 1
                )
                UPDATE stage3_grid_generation_runs run
                SET metadata = jsonb_set(
                    metadata,
                    '{region_geometry_hash}',
                    to_jsonb(CAST(:source_hash AS text)),
                    true
                )
                FROM latest
                WHERE run.id = latest.id
                """
            ),
            {"region": REGION, "grid": GRID, "source_hash": source_hash},
        )
        db.commit()
        print(source_hash)


if __name__ == "__main__":
    main()
