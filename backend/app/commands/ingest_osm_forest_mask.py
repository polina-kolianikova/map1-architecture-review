from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import requests
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.source_strategy import SOURCE_LAYER_OSM_FOREST, source_definition


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OSM_TAGS = ("natural=wood", "landuse=forest")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest public OSM forest polygons as an auxiliary forest mask")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--bbox", default="", help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--bbox-from-region", action="store_true")
    parser.add_argument("--overpass-url", default=",".join(OVERPASS_FALLBACK_URLS))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--min-area-ha", type=float, default=0.05)
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
        features = osm_forest_features(elements)
        if args.dry_run:
            print(
                "osm forest mask dry run "
                f"region_code={args.region_code} bbox={','.join(str(round(value, 6)) for value in bbox)} "
                f"features={len(features)}"
            )
            return

        stored = insert_osm_forest_features(
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
            error=None,
            timeout=args.timeout,
            overpass_url=args.overpass_url,
        )
        db.commit()

    if stored <= 0:
        raise SystemExit("no OSM forest polygons were stored for the requested region")
    print(f"osm forest mask stored region_code={args.region_code} features={stored}")


def _validate_args(args) -> None:
    if bool(args.bbox) == bool(args.bbox_from_region):
        raise ValueError("choose exactly one of --bbox or --bbox-from-region")
    if args.timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if args.min_area_ha < 0:
        raise ValueError("min-area-ha must not be negative")


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


def _load_region_bbox(db, region_code: str) -> tuple[float, float, float, float]:
    row = db.execute(
        text(
            """
            SELECT
                ST_XMin(ST_Envelope(geom)) AS min_lon,
                ST_YMin(ST_Envelope(geom)) AS min_lat,
                ST_XMax(ST_Envelope(geom)) AS max_lon,
                ST_YMax(ST_Envelope(geom)) AS max_lat
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).mappings().one()
    return (float(row["min_lon"]), float(row["min_lat"]), float(row["max_lon"]), float(row["max_lat"]))


def parse_overpass_urls(value: str) -> list[str]:
    urls = [part.strip() for part in value.split(",") if part.strip()]
    return urls or list(OVERPASS_FALLBACK_URLS)


def fetch_osm_elements(
    overpass_url: str | list[str],
    bbox: tuple[float, float, float, float],
    timeout: int,
) -> list[dict[str, Any]]:
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
        relation_queries = f"""
      relation["natural"="wood"]({min_lat},{min_lon},{max_lat},{max_lon});
      relation["landuse"="forest"]({min_lat},{min_lon},{max_lat},{max_lon});
        """
    query = f"""
    [out:json][timeout:{timeout}];
    (
      way["natural"="wood"]({min_lat},{min_lon},{max_lat},{max_lon});
      way["landuse"="forest"]({min_lat},{min_lon},{max_lat},{max_lon});
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


def osm_forest_features(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for element in elements:
        element_type = element.get("type")
        if element_type == "way":
            polygon = _polygon_from_way(element)
            if polygon:
                features.append(polygon)
        elif element_type == "relation":
            features.extend(_polygons_from_relation(element))
    return features


def _polygon_from_way(element: dict[str, Any]) -> dict[str, Any] | None:
    ring = _ring_from_geometry(element.get("geometry") or [])
    if not ring:
        return None
    return _feature(
        source_id=f"osm:{element.get('type')}:{element.get('id')}",
        tags=element.get("tags") or {},
        geometry={"type": "Polygon", "coordinates": [ring]},
    )


def _polygons_from_relation(element: dict[str, Any]) -> list[dict[str, Any]]:
    outer_segments = []
    inner_segments = []
    for member in element.get("members") or []:
        role = member.get("role")
        coordinates = _coordinates_from_geometry(member.get("geometry") or [])
        if len(coordinates) < 2:
            continue
        if role == "inner":
            inner_segments.append(coordinates)
        else:
            outer_segments.append(coordinates)
    outer_rings = _assemble_rings(outer_segments)
    inner_rings = _assemble_rings(inner_segments)
    features = []
    for index, outer in enumerate(outer_rings):
        holes = [inner for inner in inner_rings if _bbox_contains(_bbox(outer), _bbox(inner))]
        features.append(
            _feature(
                source_id=f"osm:{element.get('type')}:{element.get('id')}:{index}",
                tags=element.get("tags") or {},
                geometry={"type": "Polygon", "coordinates": [outer, *holes]},
            )
        )
    return features


def _feature(source_id: str, tags: dict[str, Any], geometry: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "tags": tags,
        "geometry": geometry,
        "forest_type": tags.get("natural") or tags.get("landuse") or "forest",
    }


def _ring_from_geometry(geometry: list[dict[str, Any]]) -> list[list[float]] | None:
    coordinates = _coordinates_from_geometry(geometry)
    if len(coordinates) < 4:
        return None
    if _point_key(coordinates[0]) != _point_key(coordinates[-1]):
        return None
    return coordinates


def _coordinates_from_geometry(geometry: list[dict[str, Any]]) -> list[list[float]]:
    coordinates = []
    for point in geometry:
        if "lon" not in point or "lat" not in point:
            continue
        coordinates.append([float(point["lon"]), float(point["lat"])])
    return coordinates


def _assemble_rings(segments: list[list[list[float]]]) -> list[list[list[float]]]:
    remaining = [segment[:] for segment in segments if len(segment) >= 2]
    rings: list[list[list[float]]] = []
    while remaining:
        ring = remaining.pop(0)
        changed = True
        while changed and _point_key(ring[0]) != _point_key(ring[-1]):
            changed = False
            for index, segment in enumerate(remaining):
                if _point_key(ring[-1]) == _point_key(segment[0]):
                    ring.extend(segment[1:])
                elif _point_key(ring[-1]) == _point_key(segment[-1]):
                    ring.extend(reversed(segment[:-1]))
                elif _point_key(ring[0]) == _point_key(segment[-1]):
                    ring = segment[:-1] + ring
                elif _point_key(ring[0]) == _point_key(segment[0]):
                    ring = list(reversed(segment[1:])) + ring
                else:
                    continue
                remaining.pop(index)
                changed = True
                break
        closed_ring = _ring_from_coordinates(ring)
        if closed_ring:
            rings.append(closed_ring)
    return rings


def _ring_from_coordinates(coordinates: list[list[float]]) -> list[list[float]] | None:
    if len(coordinates) < 4:
        return None
    if _point_key(coordinates[0]) != _point_key(coordinates[-1]):
        return None
    return coordinates


def _point_key(point: list[float]) -> tuple[float, float]:
    return round(point[0], 7), round(point[1], 7)


def _bbox(ring: list[list[float]]) -> tuple[float, float, float, float]:
    longitudes = [point[0] for point in ring]
    latitudes = [point[1] for point in ring]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def insert_osm_forest_features(
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
                valid_geometry AS (
                    SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)) AS geom
                    FROM source_geometry
                ),
                clipped_geometry AS (
                    SELECT
                        ST_Multi(
                            ST_CollectionExtract(
                                ST_MakeValid(ST_Intersection(valid_geometry.geom, region.geom)),
                                3
                            )
                        ) AS geom
                    FROM valid_geometry
                    JOIN regions region ON region.id = :region_id
                    WHERE ST_Intersects(valid_geometry.geom, region.geom)
                ),
                measurable AS (
                    SELECT geom, ST_Area(ST_Transform(geom, 6933)) / 10000.0 AS area_ha
                    FROM clipped_geometry
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
                    :forest_type,
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
                "region_id": region["id"],
                "source_id": feature["source_id"],
                "source_layer": SOURCE_LAYER_OSM_FOREST,
                "forest_type": feature["forest_type"],
                "official_status": "public_osm_forest",
                "attributes": json.dumps(
                    {
                        "pipeline": "stage3_osm_forest_mask",
                        "region_id": region["id"],
                        "region_code": region["code"],
                        "source_url": overpass_url,
                        "source_definition": source_definition(SOURCE_LAYER_OSM_FOREST),
                        "source_checked_at": checked_at,
                        "request_timeout_seconds": timeout,
                        "osm_tags_used": OSM_TAGS,
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
            {"source_layer": SOURCE_LAYER_OSM_FOREST, "region_code": region["code"]},
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
    error: str | None,
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
                :error,
                CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "source_layer": SOURCE_LAYER_OSM_FOREST,
            "source_type": "osm_overpass",
            "region_code": region_code,
            "status": status,
            "feature_count": feature_count,
            "error": error,
            "metadata": json.dumps(
                {
                    "pipeline": "stage3_osm_forest_source_check",
                    "request_timeout_seconds": timeout,
                    "source_url": overpass_url,
                    "osm_tags_used": OSM_TAGS,
                },
                separators=(",", ":"),
            ),
        },
    )


if __name__ == "__main__":
    main()
