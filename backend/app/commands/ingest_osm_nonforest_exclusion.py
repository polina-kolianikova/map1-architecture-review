from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import requests
from sqlalchemy import text

from app.commands.ingest_osm_forest_mask import (
    OVERPASS_FALLBACK_URLS,
    _assemble_rings,
    _coordinates_from_geometry,
    _load_region_bbox,
    _polygon_from_way,
    parse_bbox,
    parse_overpass_urls,
)
from app.db.session import SessionLocal
from app.services.source_strategy import SOURCE_LAYER_OSM_NONFOREST_EXCLUSION, source_definition


POLYGON_ANY_TAGS = (
    "building",
    "amenity",
    "barrier",
    "man_made",
    "military",
    "aeroway",
    "tourism",
    "shop",
    "office",
    "craft",
)
POLYGON_VALUE_TAGS = {
    "landuse": (
        "allotments",
        "aquaculture",
        "basin",
        "residential",
        "commercial",
        "industrial",
        "retail",
        "construction",
        "farmland",
        "farmyard",
        "grass",
        "meadow",
        "orchard",
        "plant_nursery",
        "vineyard",
        "quarry",
        "brownfield",
        "cemetery",
        "garages",
        "greenhouse_horticulture",
        "landfill",
        "railway",
        "recreation_ground",
        "reservoir",
    ),
    "natural": (
        "bare_rock",
        "beach",
        "sand",
        "scree",
        "shingle",
        "water",
        "wetland",
    ),
    "water": (
        "basin",
        "canal",
        "ditch",
        "drain",
        "lake",
        "pond",
        "reservoir",
        "river",
        "stream",
    ),
    "leisure": (
        "garden",
        "golf_course",
        "pitch",
        "playground",
        "recreation_ground",
        "sports_centre",
        "stadium",
        "swimming_pool",
        "track",
    ),
}
LINE_ANY_TAGS = (
    "highway",
    "railway",
    "waterway",
)
POLYGON_EXCLUSION_TYPES = set(POLYGON_ANY_TAGS) | set(POLYGON_VALUE_TAGS)

POLYGON_LANDUSE = (
    "residential",
    "commercial",
    "industrial",
    "retail",
    "construction",
    "farmland",
    "farmyard",
    "meadow",
    "orchard",
    "vineyard",
    "quarry",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest OSM non-forest geometries for Stage 3 forest-mask difference")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--bbox", default="", help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--bbox-from-region", action="store_true")
    parser.add_argument("--overpass-url", default=",".join(OVERPASS_FALLBACK_URLS))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--line-buffer-m", type=float, default=8.0)
    parser.add_argument("--min-area-ha", type=float, default=0.001)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    _validate_args(args)

    with SessionLocal() as db:
        region = db.execute(
            text("SELECT id, code FROM regions WHERE code = :region_code"),
            {"region_code": args.region_code},
        ).mappings().one()
        bbox = _load_region_bbox(db, args.region_code) if args.bbox_from_region else parse_bbox(args.bbox)
        elements = fetch_osm_elements(parse_overpass_urls(args.overpass_url), bbox, args.timeout)
        features = osm_exclusion_features(elements, line_buffer_m=args.line_buffer_m)
        if args.dry_run:
            print(
                "osm non-forest exclusion dry run "
                f"region_code={args.region_code} bbox={','.join(str(round(value, 6)) for value in bbox)} "
                f"features={len(features)}"
            )
            return
        stored = insert_exclusion_features(
            db,
            region=region,
            features=features,
            overpass_url=args.overpass_url,
            timeout=args.timeout,
            min_area_ha=args.min_area_ha,
        )
        _record_source_check(
            db,
            region_code=region["code"],
            status="ok" if stored else "empty",
            feature_count=stored,
            timeout=args.timeout,
            overpass_url=args.overpass_url,
        )
        db.commit()

    if stored <= 0:
        raise SystemExit("no OSM non-forest exclusion geometries were stored for the requested region")
    print(f"osm non-forest exclusion stored region_code={args.region_code} features={stored}")


def _validate_args(args) -> None:
    if bool(args.bbox) == bool(args.bbox_from_region):
        raise ValueError("choose exactly one of --bbox or --bbox-from-region")
    if args.timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if args.line_buffer_m < 0:
        raise ValueError("line-buffer-m must not be negative")
    if args.min_area_ha < 0:
        raise ValueError("min-area-ha must not be negative")


def fetch_osm_elements(overpass_url: str | list[str], bbox: tuple[float, float, float, float], timeout: int) -> list[dict[str, Any]]:
    try:
        return _fetch_osm_elements_with_mode(overpass_url, bbox, timeout, include_relations=True)
    except requests.RequestException:
        return _fetch_osm_elements_with_mode(overpass_url, bbox, timeout, include_relations=False)


def _fetch_osm_elements_with_mode(
    overpass_url: str | list[str],
    bbox: tuple[float, float, float, float],
    timeout: int,
    *,
    include_relations: bool,
) -> list[dict[str, Any]]:
    urls = [overpass_url] if isinstance(overpass_url, str) else overpass_url
    last_error: Exception | None = None
    for url in urls:
        try:
            return _fetch_osm_elements_from_url(url, bbox, timeout, include_relations=include_relations)
        except requests.RequestException as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("no Overpass endpoint is configured")


def _fetch_osm_elements_from_url(
    overpass_url: str,
    bbox: tuple[float, float, float, float],
    timeout: int,
    *,
    include_relations: bool = True,
) -> list[dict[str, Any]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    relation_queries = ""
    if include_relations:
        relation_queries = "\n".join(
            _overpass_selector("relation", key, values, min_lat, min_lon, max_lat, max_lon)
            for key, values in _polygon_selectors()
        )
    polygon_way_queries = "\n".join(
        _overpass_selector("way", key, values, min_lat, min_lon, max_lat, max_lon)
        for key, values in _polygon_selectors()
    )
    line_way_queries = "\n".join(
        _overpass_selector("way", key, None, min_lat, min_lon, max_lat, max_lon)
        for key in LINE_ANY_TAGS
    )
    query = f"""
    [out:json][timeout:{timeout}];
    (
      {line_way_queries}
      {polygon_way_queries}
      {relation_queries}
    );
    out body geom;
    """
    response = requests.post(
        overpass_url,
        data={"data": query},
        headers={"User-Agent": "map1-turkey-stage3/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    elements = payload.get("elements") or []
    if not isinstance(elements, list):
        raise RuntimeError("Overpass response does not contain an elements list")
    return elements


def _polygon_selectors() -> list[tuple[str, tuple[str, ...] | None]]:
    selectors = [(key, None) for key in POLYGON_ANY_TAGS]
    selectors.extend((key, values) for key, values in POLYGON_VALUE_TAGS.items())
    return selectors


def _overpass_selector(
    element_type: str,
    key: str,
    values: tuple[str, ...] | None,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> str:
    if values:
        pattern = "|".join(values)
        return f'{element_type}["{key}"~"^({pattern})$"]({min_lat},{min_lon},{max_lat},{max_lon});'
    return f'{element_type}["{key}"]({min_lat},{min_lon},{max_lat},{max_lon});'


def osm_exclusion_features(elements: list[dict[str, Any]], *, line_buffer_m: float) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for element in elements:
        tags = element.get("tags") or {}
        element_type = element.get("type")
        exclusion_type = _exclusion_type(tags)
        if not exclusion_type:
            continue
        if element_type == "way":
            feature = _way_feature(element, exclusion_type, line_buffer_m=line_buffer_m)
            if feature:
                features.append(feature)
        elif element_type == "relation":
            features.extend(_relation_features(element, exclusion_type))
    return features


def _way_feature(element: dict[str, Any], exclusion_type: str, *, line_buffer_m: float) -> dict[str, Any] | None:
    if exclusion_type in POLYGON_EXCLUSION_TYPES:
        polygon = _polygon_from_way(element)
        if polygon:
            return _feature(
                source_id=f"osm:{element.get('type')}:{element.get('id')}",
                tags=element.get("tags") or {},
                geometry=polygon["geometry"],
                exclusion_type=exclusion_type,
                buffer_m=0,
            )
    coordinates = _coordinates_from_geometry(element.get("geometry") or [])
    if len(coordinates) < 2:
        return None
    return _feature(
        source_id=f"osm:{element.get('type')}:{element.get('id')}",
        tags=element.get("tags") or {},
        geometry={"type": "LineString", "coordinates": coordinates},
        exclusion_type=exclusion_type,
        buffer_m=line_buffer_m,
    )


def _relation_features(element: dict[str, Any], exclusion_type: str) -> list[dict[str, Any]]:
    if exclusion_type not in POLYGON_EXCLUSION_TYPES:
        return []
    outer_segments = []
    inner_segments = []
    for member in element.get("members") or []:
        coordinates = _coordinates_from_geometry(member.get("geometry") or [])
        if len(coordinates) < 2:
            continue
        if member.get("role") == "inner":
            inner_segments.append(coordinates)
        else:
            outer_segments.append(coordinates)
    outer_rings = _assemble_rings(outer_segments)
    inner_rings = _assemble_rings(inner_segments)
    features = []
    for index, outer in enumerate(outer_rings):
        features.append(
            _feature(
                source_id=f"osm:{element.get('type')}:{element.get('id')}:{index}",
                tags=element.get("tags") or {},
                geometry={"type": "Polygon", "coordinates": [outer, *inner_rings]},
                exclusion_type=exclusion_type,
                buffer_m=0,
            )
        )
    return features


def _feature(
    *,
    source_id: str,
    tags: dict[str, Any],
    geometry: dict[str, Any],
    exclusion_type: str,
    buffer_m: float,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "tags": tags,
        "geometry": geometry,
        "exclusion_type": exclusion_type,
        "buffer_m": buffer_m,
    }


def _exclusion_type(tags: dict[str, Any]) -> str:
    for key in POLYGON_ANY_TAGS:
        if tags.get(key):
            return key
    for key, values in POLYGON_VALUE_TAGS.items():
        if tags.get(key) in values:
            return key
    if tags.get("highway"):
        return "road"
    if tags.get("railway"):
        return "railway"
    if tags.get("waterway"):
        return "waterway"
    return ""


def insert_exclusion_features(
    db,
    *,
    region,
    features: list[dict[str, Any]],
    overpass_url: str,
    timeout: int,
    min_area_ha: float,
) -> int:
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    inserted_ids = []
    for feature in features:
        row_id = db.execute(
            text(
                """
                WITH source_geometry AS (
                    SELECT ST_SetSRID(ST_GeomFromGeoJSON(:geometry), 4326) AS geom
                ),
                prepared AS (
                    SELECT
                        CASE
                            WHEN GeometryType(geom) IN ('LINESTRING', 'MULTILINESTRING') THEN
                                ST_Transform(
                                    ST_Buffer(ST_Transform(geom, :projected_srid), :buffer_m),
                                    4326
                                )
                            ELSE geom
                        END AS geom
                    FROM source_geometry
                ),
                clipped AS (
                    SELECT ST_Multi(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection(prepared.geom, region.geom)),
                            3
                        )
                    ) AS geom
                    FROM prepared
                    JOIN regions region ON region.id = :region_id
                    WHERE ST_Intersects(prepared.geom, region.geom)
                ),
                measurable AS (
                    SELECT geom, ST_Area(ST_Transform(geom, :projected_srid)) / 10000.0 AS area_ha
                    FROM clipped
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                      AND ST_IsValid(geom)
                )
                INSERT INTO ogm_parcels (
                    source_id,
                    source_layer,
                    forest_type,
                    tree_age,
                    official_status,
                    attributes,
                    geom,
                    last_seen_at,
                    is_current
                )
                SELECT
                    :source_id,
                    :source_layer,
                    :exclusion_type,
                    NULL,
                    :official_status,
                    CAST(:attributes AS jsonb),
                    geom,
                    now(),
                    false
                FROM measurable
                WHERE area_ha >= :min_area_ha
                RETURNING id
                """
            ),
            {
                "geometry": json.dumps(feature["geometry"], separators=(",", ":")),
                "projected_srid": 32636,
                "buffer_m": feature["buffer_m"],
                "region_id": region["id"],
                "source_id": feature["source_id"],
                "source_layer": SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
                "exclusion_type": feature["exclusion_type"],
                "official_status": "public_osm_nonforest_exclusion",
                "attributes": json.dumps(
                    {
                        "pipeline": "stage3_osm_nonforest_exclusion",
                        "region_id": region["id"],
                        "region_code": region["code"],
                        "source_url": overpass_url,
                        "source_definition": source_definition(SOURCE_LAYER_OSM_NONFOREST_EXCLUSION),
                        "source_checked_at": checked_at,
                        "request_timeout_seconds": timeout,
                        "buffer_m": feature["buffer_m"],
                        "source_attributes": feature["tags"],
                    },
                    separators=(",", ":"),
                ),
                "min_area_ha": min_area_ha,
            },
        ).scalar_one_or_none()
        if row_id is not None:
            inserted_ids.append(row_id)
    if inserted_ids:
        db.execute(
            text(
                """
                UPDATE ogm_parcels
                SET is_current = false
                WHERE source_layer = :source_layer
                  AND attributes->>'region_code' = :region_code
                """
            ),
            {"source_layer": SOURCE_LAYER_OSM_NONFOREST_EXCLUSION, "region_code": region["code"]},
        )
        db.execute(
            text("UPDATE ogm_parcels SET is_current = true WHERE id = ANY(:inserted_ids)"),
            {"inserted_ids": inserted_ids},
        )
    return len(inserted_ids)


def _record_source_check(
    db,
    *,
    region_code: str,
    status: str,
    feature_count: int,
    timeout: int,
    overpass_url: str,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO source_checks (
                source_layer,
                source_type,
                region_code,
                status,
                feature_count,
                error,
                metadata
            )
            VALUES (
                :source_layer,
                :source_type,
                :region_code,
                :status,
                :feature_count,
                NULL,
                CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "source_layer": SOURCE_LAYER_OSM_NONFOREST_EXCLUSION,
            "source_type": "osm_overpass",
            "region_code": region_code,
            "status": status,
            "feature_count": feature_count,
            "metadata": json.dumps(
                {
                    "pipeline": "stage3_osm_nonforest_exclusion_source_check",
                    "request_timeout_seconds": timeout,
                    "source_url": overpass_url,
                },
                separators=(",", ":"),
            ),
        },
    )


if __name__ == "__main__":
    main()
