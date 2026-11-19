from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.forest_scope import (
    FILTERED_FOREST_REGION_SOURCE,
    forest_region_metadata,
    validate_auxiliary_forest_mask_source_layers,
    validate_auxiliary_nonforest_exclusion_source_layers,
    validate_forest_mask_source_layers,
)
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_PROJECTED_SRID,
    require_stage3_public_v9_full_formula,
    require_user_approved_full_turkey,
)


DEFAULT_PROJECTED_SRID = 6933
DEFAULT_TILE_SIZE_M = 10_000
DEFAULT_EDGE_TOLERANCE_M = 0.02
DEFAULT_SNAP_GRID_M = 0.001
DEFAULT_SUBDIVIDE_VERTICES = 256


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a Stage 3 clean forest mask as globally filtered connected "
            "component fragments. This keeps the client semantics of one final "
            "clean mask while avoiding a national-scale dissolve."
        )
    )
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--country-code", default="TR")
    parser.add_argument("--source-region-code", required=True)
    parser.add_argument(
        "--clip-region-code",
        default="",
        help="optional QA/full-scope clip region; defaults to source-region-code",
    )
    parser.add_argument("--source-layers", required=True)
    parser.add_argument("--intersect-mask-source-layers", default="")
    parser.add_argument("--difference-mask-source-layers", required=True)
    parser.add_argument("--projected-srid", type=int, default=DEFAULT_PROJECTED_SRID)
    parser.add_argument("--tile-size-m", type=int, default=DEFAULT_TILE_SIZE_M)
    parser.add_argument("--min-area-ha", type=float, default=0)
    parser.add_argument("--max-area-ha", type=float, default=0)
    parser.add_argument("--min-mask-component-area-ha", type=float, default=1.0)
    parser.add_argument("--edge-tolerance-m", type=float, default=DEFAULT_EDGE_TOLERANCE_M)
    parser.add_argument("--snap-grid-m", type=float, default=DEFAULT_SNAP_GRID_M)
    parser.add_argument("--subdivide-max-vertices", type=int, default=DEFAULT_SUBDIVIDE_VERTICES)
    parser.add_argument(
        "--ack-full-turkey-user-approved",
        action="store_true",
        help="required for full Turkey V10 scaling after explicit user approval",
    )
    args = parser.parse_args()
    _validate_args(args)

    source_layers = _parse_layers(args.source_layers)
    intersect_layers = _parse_layers(args.intersect_mask_source_layers)
    difference_layers = _parse_layers(args.difference_mask_source_layers)
    validate_forest_mask_source_layers(source_layers)
    if intersect_layers:
        validate_auxiliary_forest_mask_source_layers(intersect_layers)
    validate_auxiliary_nonforest_exclusion_source_layers(difference_layers)

    clip_region_code = args.clip_region_code or args.source_region_code
    metadata = _fragment_region_metadata(
        region_code=args.region_code,
        source_region_code=args.source_region_code,
        clip_region_code=clip_region_code,
        source_layers=source_layers,
        intersect_layers=intersect_layers,
        difference_layers=difference_layers,
        projected_srid=args.projected_srid,
        tile_size_m=args.tile_size_m,
        min_mask_component_area_ha=args.min_mask_component_area_ha,
        edge_tolerance_m=args.edge_tolerance_m,
        snap_grid_m=args.snap_grid_m,
    )

    with SessionLocal() as db:
        scope_area_ha = _region_area_ha(db, clip_region_code)
        require_stage3_public_v9_full_formula(
            region_code=args.region_code,
            source_region_code=args.source_region_code,
            clip_region_code=clip_region_code,
            source_layers=source_layers,
            intersect_source_layers=intersect_layers,
            difference_source_layers=difference_layers,
            min_mask_component_area_ha=args.min_mask_component_area_ha,
            scope_area_ha=scope_area_ha,
        )
        require_user_approved_full_turkey(
            approved=args.ack_full_turkey_user_approved,
            region_code=args.region_code,
            source_region_code=args.source_region_code,
            clip_region_code=clip_region_code,
            scope_area_ha=scope_area_ha,
        )
        _require_source_cache(
            db,
            source_region_code=args.source_region_code,
            source_layers=source_layers + intersect_layers + difference_layers,
            projected_srid=args.projected_srid,
        )
        _stage_tiles_and_pieces(
            db,
            source_region_code=args.source_region_code,
            clip_region_code=clip_region_code,
            source_layers=source_layers,
            intersect_layers=intersect_layers,
            difference_layers=difference_layers,
            projected_srid=args.projected_srid,
            tile_size_m=args.tile_size_m,
            snap_grid_m=args.snap_grid_m,
            subdivide_max_vertices=args.subdivide_max_vertices,
        )
        component_state = _build_connected_components(
            db,
            min_mask_component_area_ha=args.min_mask_component_area_ha,
            edge_tolerance_m=args.edge_tolerance_m,
        )
        result = _upsert_fragment_region(
            db,
            region_code=args.region_code,
            region_name=args.region_name or args.region_code,
            country_code=args.country_code,
            mask_version=args.region_code,
            projected_srid=args.projected_srid,
            metadata=metadata,
        )
        _validate_area(result["area_ha"], args.min_area_ha, args.max_area_ha)
        db.commit()

    report = {
        **result,
        **component_state,
        "region_code": args.region_code,
        "source_region_code": args.source_region_code,
        "clip_region_code": clip_region_code,
        "source_layers": source_layers,
        "intersect_source_layers": intersect_layers,
        "difference_source_layers": difference_layers,
        "projected_srid": args.projected_srid,
        "tile_size_m": args.tile_size_m,
        "min_mask_component_area_ha": args.min_mask_component_area_ha,
        "edge_tolerance_m": args.edge_tolerance_m,
        "snap_grid_m": args.snap_grid_m,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _validate_args(args: argparse.Namespace) -> None:
    if args.projected_srid <= 0:
        raise ValueError("projected-srid must be greater than zero")
    if args.tile_size_m <= 0:
        raise ValueError("tile-size-m must be greater than zero")
    if args.min_area_ha < 0:
        raise ValueError("min-area-ha must not be negative")
    if args.max_area_ha < 0:
        raise ValueError("max-area-ha must not be negative")
    if args.max_area_ha and args.min_area_ha and args.max_area_ha < args.min_area_ha:
        raise ValueError("max-area-ha must be greater than or equal to min-area-ha")
    if args.min_mask_component_area_ha < 0:
        raise ValueError("min-mask-component-area-ha must not be negative")
    if args.edge_tolerance_m < 0:
        raise ValueError("edge-tolerance-m must not be negative")
    if args.snap_grid_m < 0:
        raise ValueError("snap-grid-m must not be negative")
    if args.subdivide_max_vertices < 5:
        raise ValueError("subdivide-max-vertices must be at least 5")


def _parse_layers(value: str | list[str]) -> list[str]:
    raw = value if isinstance(value, list) else value.split(",")
    return [str(item).strip() for item in raw if str(item).strip()]


def _validate_area(area_ha: float, min_area_ha: float = 0, max_area_ha: float = 0) -> None:
    if min_area_ha and area_ha < min_area_ha:
        raise RuntimeError(f"prepared clean mask area {area_ha:.2f} ha is below minimum {min_area_ha:.2f} ha")
    if max_area_ha and area_ha > max_area_ha:
        raise RuntimeError(f"prepared clean mask area {area_ha:.2f} ha is above maximum {max_area_ha:.2f} ha")


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


def _fragment_region_metadata(
    *,
    region_code: str,
    source_region_code: str,
    clip_region_code: str,
    source_layers: list[str],
    intersect_layers: list[str],
    difference_layers: list[str],
    projected_srid: int,
    tile_size_m: int,
    min_mask_component_area_ha: float,
    edge_tolerance_m: float,
    snap_grid_m: float,
) -> dict[str, Any]:
    metadata = forest_region_metadata(
        source_region_code=source_region_code,
        source_layers=source_layers,
        intersect_source_layers=intersect_layers,
        difference_source_layers=difference_layers,
        min_mask_component_area_ha=min_mask_component_area_ha,
    )
    metadata["stage3_region_source"] = FILTERED_FOREST_REGION_SOURCE
    forest_mask = metadata["forest_mask"]
    forest_mask["mask_version"] = region_code
    forest_mask["mask_components_table"] = "stage3_region_mask_components"
    forest_mask["component_storage"] = "connected_component_fragments"
    forest_mask["component_id_metadata_key"] = "connected_component_id"
    forest_mask["component_area_metadata_key"] = "connected_component_area_ha"
    forest_mask["source_mask_cache"] = {
        "enabled": True,
        "source_region_code": source_region_code,
        "projected_srid": projected_srid,
        "source_layers": source_layers + intersect_layers + difference_layers,
    }
    forest_mask["fragmented_component_mask"] = {
        "tile_size_m": tile_size_m,
        "edge_tolerance_m": edge_tolerance_m,
        "snap_grid_m": snap_grid_m,
        "global_connected_component_filter": True,
        "min_component_area_ha": min_mask_component_area_ha,
    }
    if clip_region_code != source_region_code:
        forest_mask["clip_region_code"] = clip_region_code
        forest_mask["qa_window"] = True
    if min_mask_component_area_ha > 0:
        forest_mask["clean_mask_policy"] = {
            "method": "global_connected_component_area_filter_before_grid_generation",
            "min_component_area_ha": float(min_mask_component_area_ha),
            "applies_before_grid_insert": True,
            "client_decision": "exclude_tiny_nonforest_or_fragmented_scraps_before_stage3_acceptance",
        }
    return metadata


def _require_source_cache(
    db,
    *,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
) -> None:
    rows = db.execute(
        text(
            """
            SELECT
                source_layer,
                count(*)::bigint AS components,
                count(*) FILTER (
                    WHERE geom_projected IS NULL
                       OR ST_IsEmpty(geom_projected)
                       OR NOT ST_IsValid(geom_projected)
                )::bigint AS bad_components
            FROM stage3_source_mask_cache
            WHERE source_region_code = :source_region_code
              AND projected_srid = :projected_srid
              AND source_layer = ANY(:source_layers)
            GROUP BY source_layer
            """
        ),
        {
            "source_region_code": source_region_code,
            "source_layers": source_layers,
            "projected_srid": projected_srid,
        },
    ).mappings()
    by_layer = {str(row["source_layer"]): row for row in rows}
    missing = [layer for layer in source_layers if int(by_layer.get(layer, {}).get("components", 0) or 0) <= 0]
    bad = [layer for layer in source_layers if int(by_layer.get(layer, {}).get("bad_components", 0) or 0) > 0]
    if missing:
        raise RuntimeError(f"Stage 3 source mask cache is missing layer(s): {', '.join(missing)}")
    if bad:
        raise RuntimeError(f"Stage 3 source mask cache has invalid component(s): {', '.join(bad)}")


def _stage_tiles_and_pieces(
    db,
    *,
    source_region_code: str,
    clip_region_code: str,
    source_layers: list[str],
    intersect_layers: list[str],
    difference_layers: list[str],
    projected_srid: int,
    tile_size_m: int,
    snap_grid_m: float,
    subdivide_max_vertices: int,
) -> None:
    db.execute(text("SET LOCAL work_mem = '256MB'"))
    for table in (
        "stage3_fragment_tiles",
        "stage3_fragment_forest_tiles",
        "stage3_fragment_intersect_tiles",
        "stage3_fragment_difference_tiles",
        "stage3_fragment_refined_tiles",
        "stage3_fragment_pieces",
        "stage3_fragment_accepted_pieces",
    ):
        db.execute(text(f"DROP TABLE IF EXISTS {table}"))

    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_fragment_tiles ON COMMIT DROP AS
            WITH clip_region AS (
                SELECT ST_Transform(ST_MakeValid(geom), :projected_srid) AS geom
                FROM regions
                WHERE code = :clip_region_code
            ),
            extent AS (
                SELECT
                    floor(ST_XMin(ST_Envelope(geom)) / :tile_size_m)::integer AS min_i,
                    floor(ST_YMin(ST_Envelope(geom)) / :tile_size_m)::integer AS min_j,
                    ceil(ST_XMax(ST_Envelope(geom)) / :tile_size_m)::integer AS max_i,
                    ceil(ST_YMax(ST_Envelope(geom)) / :tile_size_m)::integer AS max_j,
                    geom AS clip_geom
                FROM clip_region
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
            ),
            raw_tiles AS (
                SELECT
                    i,
                    j,
                    ST_MakeEnvelope(
                        i * :tile_size_m,
                        j * :tile_size_m,
                        (i + 1) * :tile_size_m,
                        (j + 1) * :tile_size_m,
                        :projected_srid
                    ) AS envelope,
                    clip_geom
                FROM extent,
                     generate_series(min_i, max_i - 1) AS i,
                     generate_series(min_j, max_j - 1) AS j
            )
            SELECT
                i,
                j,
                ST_CollectionExtract(
                    ST_MakeValid(ST_Intersection(envelope, clip_geom)),
                    3
                ) AS geom
            FROM raw_tiles
            WHERE envelope && clip_geom
              AND ST_Intersects(envelope, clip_geom)
            """
        ),
        {
            "clip_region_code": clip_region_code,
            "projected_srid": projected_srid,
            "tile_size_m": tile_size_m,
        },
    )
    db.execute(text("CREATE INDEX stage3_fragment_tiles_key_idx ON stage3_fragment_tiles (i, j)"))
    db.execute(text("CREATE INDEX stage3_fragment_tiles_gix ON stage3_fragment_tiles USING GIST (geom)"))
    db.execute(text("ANALYZE stage3_fragment_tiles"))
    _create_dissolved_tile_stage(
        db,
        table_name="stage3_fragment_forest_tiles",
        source_region_code=source_region_code,
        source_layers=source_layers,
        projected_srid=projected_srid,
        snap_grid_m=snap_grid_m,
    )
    if intersect_layers:
        _create_dissolved_tile_stage(
            db,
            table_name="stage3_fragment_intersect_tiles",
            source_region_code=source_region_code,
            source_layers=intersect_layers,
            projected_srid=projected_srid,
            snap_grid_m=snap_grid_m,
        )
    else:
        db.execute(
            text(
                """
                CREATE TEMP TABLE stage3_fragment_intersect_tiles ON COMMIT DROP AS
                SELECT i, j, geom
                FROM stage3_fragment_tiles
                WHERE geom IS NOT NULL
                  AND NOT ST_IsEmpty(geom)
                """
            )
        )
        db.execute(text("CREATE INDEX stage3_fragment_intersect_tiles_key_idx ON stage3_fragment_intersect_tiles (i, j)"))
        db.execute(text("ANALYZE stage3_fragment_intersect_tiles"))
    _create_dissolved_tile_stage(
        db,
        table_name="stage3_fragment_difference_tiles",
        source_region_code=source_region_code,
        source_layers=difference_layers,
        projected_srid=projected_srid,
        snap_grid_m=snap_grid_m,
    )
    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_fragment_refined_tiles ON COMMIT DROP AS
            SELECT
                forest.i,
                forest.j,
                ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            CASE
                                WHEN intersect_mask.geom IS NULL
                                  OR ST_IsEmpty(intersect_mask.geom)
                                THEN ST_SetSRID(ST_GeomFromText('POLYGON EMPTY'), :projected_srid)
                                WHEN difference.geom IS NULL
                                  OR ST_IsEmpty(difference.geom)
                                THEN ST_Intersection(forest.geom, intersect_mask.geom)
                                ELSE ST_Difference(
                                    ST_Intersection(forest.geom, intersect_mask.geom),
                                    difference.geom
                                )
                            END
                        ),
                        3
                    )
                ) AS geom
            FROM stage3_fragment_forest_tiles forest
            LEFT JOIN stage3_fragment_intersect_tiles intersect_mask
              ON intersect_mask.i = forest.i
             AND intersect_mask.j = forest.j
            LEFT JOIN stage3_fragment_difference_tiles difference
              ON difference.i = forest.i
             AND difference.j = forest.j
            WHERE forest.geom IS NOT NULL
              AND NOT ST_IsEmpty(forest.geom)
            """
        ),
        {"projected_srid": projected_srid},
    )
    db.execute(text("CREATE INDEX stage3_fragment_refined_tiles_key_idx ON stage3_fragment_refined_tiles (i, j)"))
    db.execute(text("ANALYZE stage3_fragment_refined_tiles"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_fragment_pieces ON COMMIT DROP AS
            SELECT
                row_number() OVER (
                    ORDER BY refined.i,
                             refined.j,
                             ST_Area(piece.geom) DESC,
                             ST_AsEWKB(
                                 CASE
                                     WHEN :snap_grid_m > 0 THEN ST_SnapToGrid(piece.geom, :snap_grid_m)
                                     ELSE piece.geom
                                 END
                             )::text
                )::bigint AS id,
                refined.i,
                refined.j,
                ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(part.geom),
                        3
                    )
                ) AS geom,
                ST_Area(part.geom)::float AS area_m2
            FROM stage3_fragment_refined_tiles refined
            CROSS JOIN LATERAL ST_Dump(
                ST_CollectionExtract(ST_MakeValid(refined.geom), 3)
            ) AS piece
            CROSS JOIN LATERAL ST_Subdivide(
                ST_CollectionExtract(
                    ST_MakeValid(
                        CASE
                            WHEN :snap_grid_m > 0 THEN ST_SnapToGrid(piece.geom, :snap_grid_m)
                            ELSE piece.geom
                        END
                    ),
                    3
                ),
                :subdivide_max_vertices
            ) AS part(geom)
            WHERE piece.geom IS NOT NULL
              AND NOT ST_IsEmpty(piece.geom)
              AND part.geom IS NOT NULL
              AND NOT ST_IsEmpty(part.geom)
              AND ST_Area(part.geom) > 0
            """
        ),
        {
            "snap_grid_m": snap_grid_m,
            "subdivide_max_vertices": subdivide_max_vertices,
        },
    )
    db.execute(text("ALTER TABLE stage3_fragment_pieces ADD PRIMARY KEY (id)"))
    db.execute(text("CREATE INDEX stage3_fragment_pieces_tile_idx ON stage3_fragment_pieces (i, j)"))
    db.execute(text("CREATE INDEX stage3_fragment_pieces_gix ON stage3_fragment_pieces USING GIST (geom)"))
    db.execute(text("ANALYZE stage3_fragment_pieces"))


def _create_dissolved_tile_stage(
    db,
    *,
    table_name: str,
    source_region_code: str,
    source_layers: list[str],
    projected_srid: int,
    snap_grid_m: float,
) -> None:
    if snap_grid_m <= 0:
        union_sql = """
            ST_UnaryUnion(
                ST_Collect(
                    ST_CollectionExtract(
                        ST_MakeValid(geom),
                        3
                    )
                )
            )
        """
    else:
        union_sql = """
            ST_UnaryUnion(
                ST_Collect(
                    ST_CollectionExtract(
                        ST_MakeValid(ST_SnapToGrid(geom, :snap_grid_m)),
                        3
                    )
                ),
                :snap_grid_m
            )
        """
    db.execute(
        text(
            f"""
            CREATE TEMP TABLE {table_name} ON COMMIT DROP AS
            WITH clipped AS (
                SELECT
                    tile.i,
                    tile.j,
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_Intersection(cache.geom_projected, tile.geom)
                        ),
                        3
                    ) AS geom
                FROM stage3_fragment_tiles tile
                JOIN stage3_source_mask_cache cache
                  ON cache.source_region_code = :source_region_code
                 AND cache.projected_srid = :projected_srid
                 AND cache.source_layer = ANY(:source_layers)
                 AND cache.geom_projected && tile.geom
                 AND ST_Intersects(cache.geom_projected, tile.geom)
            )
            SELECT
                i,
                j,
                ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            {union_sql}
                        ),
                        3
                    )
                ) AS geom
            FROM clipped
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            GROUP BY i, j
            """
        ),
        {
            "source_region_code": source_region_code,
            "source_layers": source_layers,
            "projected_srid": projected_srid,
            "snap_grid_m": snap_grid_m,
        },
    )
    db.execute(text(f"CREATE INDEX {table_name}_key_idx ON {table_name} (i, j)"))
    db.execute(text(f"ANALYZE {table_name}"))


def _build_connected_components(
    db,
    *,
    min_mask_component_area_ha: float,
    edge_tolerance_m: float,
) -> dict[str, Any]:
    piece_rows = list(
        db.execute(
            text(
                """
                SELECT id, area_m2
                FROM stage3_fragment_pieces
                ORDER BY id
                """
            )
        ).mappings()
    )
    if not piece_rows:
        raise RuntimeError("no clean forest mask fragments were produced")
    parent = {int(row["id"]): int(row["id"]) for row in piece_rows}
    rank = {piece_id: 0 for piece_id in parent}

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            parent[left_root] = right_root
        elif rank[left_root] > rank[right_root]:
            parent[right_root] = left_root
        else:
            parent[right_root] = left_root
            rank[left_root] += 1

    edge_count = 0
    edge_rows = db.execute(
        text(
            """
            SELECT left_piece.id AS left_id, right_piece.id AS right_id
            FROM stage3_fragment_pieces left_piece
            JOIN stage3_fragment_pieces right_piece
              ON left_piece.id < right_piece.id
             AND abs(left_piece.i - right_piece.i) <= 1
             AND abs(left_piece.j - right_piece.j) <= 1
             AND left_piece.geom && ST_Expand(right_piece.geom, :edge_tolerance_m)
             AND ST_DWithin(left_piece.geom, right_piece.geom, :edge_tolerance_m)
            """
        ),
        {"edge_tolerance_m": edge_tolerance_m},
    )
    for row in edge_rows:
        edge_count += 1
        union(int(row.left_id), int(row.right_id))

    component_area_m2: dict[int, float] = {}
    for row in piece_rows:
        piece_id = int(row["id"])
        root = find(piece_id)
        component_area_m2[root] = component_area_m2.get(root, 0.0) + float(row["area_m2"] or 0)

    min_component_area_m2 = min_mask_component_area_ha * 10_000.0
    accepted_roots = {
        root
        for root, area_m2 in component_area_m2.items()
        if min_component_area_m2 <= 0 or area_m2 >= min_component_area_m2
    }
    if not accepted_roots:
        raise RuntimeError("all clean forest mask components were filtered out")
    component_index_by_root = {
        root: index
        for index, root in enumerate(
            sorted(accepted_roots, key=lambda item: (-component_area_m2[item], item)),
            start=1,
        )
    }
    accepted_rows = []
    rejected_pieces = 0
    rejected_area_m2 = 0.0
    for row in piece_rows:
        piece_id = int(row["id"])
        root = find(piece_id)
        area_m2 = float(row["area_m2"] or 0)
        if root not in accepted_roots:
            rejected_pieces += 1
            rejected_area_m2 += area_m2
            continue
        accepted_rows.append(
            {
                "piece_id": piece_id,
                "connected_component_id": component_index_by_root[root],
                "connected_component_area_ha": component_area_m2[root] / 10_000.0,
            }
        )

    db.execute(
        text(
            """
            CREATE TEMP TABLE stage3_fragment_accepted_pieces (
                piece_id bigint PRIMARY KEY,
                connected_component_id integer NOT NULL,
                connected_component_area_ha double precision NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    for chunk in _chunks(accepted_rows, 10_000):
        db.execute(
            text(
                """
                INSERT INTO stage3_fragment_accepted_pieces (
                    piece_id,
                    connected_component_id,
                    connected_component_area_ha
                )
                VALUES (
                    :piece_id,
                    :connected_component_id,
                    :connected_component_area_ha
                )
                """
            ),
            chunk,
        )
    db.execute(text("CREATE INDEX stage3_fragment_accepted_component_idx ON stage3_fragment_accepted_pieces (connected_component_id)"))
    db.execute(text("ANALYZE stage3_fragment_accepted_pieces"))
    accepted_area_m2 = sum(
        float(row["area_m2"] or 0)
        for row in piece_rows
        if find(int(row["id"])) in accepted_roots
    )
    return {
        "piece_count": len(piece_rows),
        "edge_count": edge_count,
        "connected_component_count": len(component_area_m2),
        "accepted_connected_component_count": len(accepted_roots),
        "accepted_piece_count": len(accepted_rows),
        "accepted_area_ha": round(accepted_area_m2 / 10_000.0, 4),
        "rejected_piece_count": rejected_pieces,
        "rejected_area_ha": round(rejected_area_m2 / 10_000.0, 4),
        "max_connected_component_area_ha": round(max(component_area_m2.values()) / 10_000.0, 4),
        "min_accepted_connected_component_area_ha": round(
            min(component_area_m2[root] for root in accepted_roots) / 10_000.0,
            4,
        ),
    }


def _upsert_fragment_region(
    db,
    *,
    region_code: str,
    region_name: str,
    country_code: str,
    mask_version: str,
    projected_srid: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            WITH final_stats AS (
                SELECT
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(
                                ST_Collect(ST_Transform(piece.geom, 4326))
                            ),
                            3
                        )
                    )::geometry(MultiPolygon, 4326) AS geom,
                    COALESCE(sum(piece.area_m2) / 10000.0, 0)::float AS area_ha
                FROM stage3_fragment_pieces piece
                JOIN stage3_fragment_accepted_pieces accepted
                  ON accepted.piece_id = piece.id
            ),
            changed AS (
                INSERT INTO regions (name, code, country_code, geom, metadata)
                SELECT
                    :name,
                    :code,
                    :country_code,
                    geom,
                    CAST(:metadata AS jsonb)
                FROM final_stats
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
                final_stats.area_ha,
                changed.metadata->>'stage3_region_source' AS source
            FROM changed
            CROSS JOIN final_stats
            """
        ),
        {
            "name": region_name,
            "code": region_code,
            "country_code": country_code,
            "metadata": json.dumps(metadata, separators=(",", ":")),
        },
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError("final clean forest mask is empty")

    region_id = int(row["id"])
    db.execute(
        text(
            """
            DELETE FROM stage3_region_mask_components
            WHERE region_id = :region_id
              AND mask_version = :mask_version
              AND projected_srid = :projected_srid
            """
        ),
        {
            "region_id": region_id,
            "mask_version": mask_version,
            "projected_srid": projected_srid,
        },
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
                row_number() OVER (
                    ORDER BY accepted.connected_component_id,
                             piece.i,
                             piece.j,
                             piece.id
                )::integer AS component_index,
                :projected_srid,
                ST_Multi(ST_Transform(piece.geom, 4326)),
                ST_Multi(piece.geom),
                round((piece.area_m2 / 10000.0)::numeric, 6),
                jsonb_build_object(
                    'pipeline', 'stage3_prepare_region_connected_component_fragments',
                    'component_storage', 'connected_component_fragments',
                    'connected_component_id', accepted.connected_component_id,
                    'connected_component_area_ha', round(accepted.connected_component_area_ha::numeric, 4),
                    'source_tile', piece.i::text || ':' || piece.j::text
                )
            FROM stage3_fragment_pieces piece
            JOIN stage3_fragment_accepted_pieces accepted
              ON accepted.piece_id = piece.id
            WHERE piece.geom IS NOT NULL
              AND NOT ST_IsEmpty(piece.geom)
            ORDER BY accepted.connected_component_id, piece.i, piece.j, piece.id
            """
        ),
        {
            "region_id": region_id,
            "mask_version": mask_version,
            "projected_srid": projected_srid,
        },
    )
    component_rows = int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM stage3_region_mask_components
                WHERE region_id = :region_id
                  AND mask_version = :mask_version
                  AND projected_srid = :projected_srid
                """
            ),
            {
                "region_id": region_id,
                "mask_version": mask_version,
                "projected_srid": projected_srid,
            },
        ).scalar_one()
    )
    return {
        "region_id": region_id,
        "area_ha": round(float(row["area_ha"] or 0), 4),
        "stored_component_fragments": component_rows,
        "source": row["source"],
    }


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


if __name__ == "__main__":
    main()
