from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.services.source_strategy import source_definition


FOREST_REGION_SOURCE = "forest_mask_layer_a"
FILTERED_FOREST_REGION_SOURCE = "filtered_final_forest_mask_components"
COMPONENT_EXTENT_ROLE = "processing_extent_components_v1"
PUBLIC_FOREST_MASK_ROLES = {"active_public_layer_a"}
PUBLIC_FOREST_MASK_QUALITIES = {"public_ogm_rest", "public_wms_derived"}
AUXILIARY_FOREST_MASK_ROLES = {"auxiliary_public_forest_mask"}
AUXILIARY_FOREST_MASK_QUALITIES = {"public_osm_forest"}
AUXILIARY_NONFOREST_EXCLUSION_ROLES = {"auxiliary_public_nonforest_exclusion"}
AUXILIARY_NONFOREST_EXCLUSION_QUALITIES = {
    "public_derived_nonforest_exclusion",
    "public_osm_nonforest_exclusion",
    "public_satellite_landcover_nonforest_exclusion",
}
FUTURE_FOREST_MASK_ROLES = {"future_full_layer_a"}
FUTURE_FOREST_MASK_QUALITIES = {"official_full_layer_a"}
ALLOWED_FOREST_MASK_ROLES = (
    PUBLIC_FOREST_MASK_ROLES
    | AUXILIARY_FOREST_MASK_ROLES
    | AUXILIARY_NONFOREST_EXCLUSION_ROLES
    | FUTURE_FOREST_MASK_ROLES
)
ALLOWED_FOREST_MASK_QUALITIES = (
    PUBLIC_FOREST_MASK_QUALITIES
    | AUXILIARY_FOREST_MASK_QUALITIES
    | AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
    | FUTURE_FOREST_MASK_QUALITIES
)


def forest_mask_source_layers() -> list[str]:
    settings = get_settings()
    source_layers = settings.forest_mask_source_layers
    validate_forest_mask_source_layers(source_layers)
    return source_layers


def validate_forest_mask_source_layers(source_layers: list[str]) -> None:
    if not source_layers:
        raise RuntimeError("Stage 3 forest mask source layers are not configured")
    invalid_layers = []
    for source_layer in source_layers:
        definition = source_definition(source_layer)
        role = definition.get("production_role")
        quality = definition.get("source_quality")
        if role not in ALLOWED_FOREST_MASK_ROLES or quality not in ALLOWED_FOREST_MASK_QUALITIES:
            invalid_layers.append(source_layer)
    if invalid_layers:
        joined = ", ".join(invalid_layers)
        raise RuntimeError(f"Stage 3 forest mask source layer is not allowed: {joined}")


def validate_auxiliary_forest_mask_source_layers(source_layers: list[str]) -> None:
    if not source_layers:
        raise RuntimeError("Stage 3 auxiliary forest mask source layers are not configured")
    invalid_layers = []
    for source_layer in source_layers:
        definition = source_definition(source_layer)
        role = definition.get("production_role")
        quality = definition.get("source_quality")
        if role not in AUXILIARY_FOREST_MASK_ROLES or quality not in AUXILIARY_FOREST_MASK_QUALITIES:
            invalid_layers.append(source_layer)
    if invalid_layers:
        joined = ", ".join(invalid_layers)
        raise RuntimeError(f"Stage 3 auxiliary forest mask source layer is not allowed: {joined}")


def validate_auxiliary_nonforest_exclusion_source_layers(source_layers: list[str]) -> None:
    if not source_layers:
        raise RuntimeError("Stage 3 auxiliary non-forest exclusion source layers are not configured")
    invalid_layers = []
    for source_layer in source_layers:
        definition = source_definition(source_layer)
        role = definition.get("production_role")
        quality = definition.get("source_quality")
        if role not in AUXILIARY_NONFOREST_EXCLUSION_ROLES or quality not in AUXILIARY_NONFOREST_EXCLUSION_QUALITIES:
            invalid_layers.append(source_layer)
    if invalid_layers:
        joined = ", ".join(invalid_layers)
        raise RuntimeError(f"Stage 3 auxiliary non-forest exclusion source layer is not allowed: {joined}")


def forest_region_metadata(
    *,
    source_region_code: str,
    source_layers: list[str],
    clip_to_grid_version: str = "",
    intersect_source_layers: list[str] | None = None,
    difference_source_layers: list[str] | None = None,
    min_mask_component_area_ha: float = 0,
) -> dict[str, Any]:
    intersect_source_layers = intersect_source_layers or []
    difference_source_layers = difference_source_layers or []
    source_details = [_source_detail(source_layer) for source_layer in source_layers]
    intersect_source_details = [_source_detail(source_layer) for source_layer in intersect_source_layers]
    difference_source_details = [_source_detail(source_layer) for source_layer in difference_source_layers]
    mask_source = forest_mask_source(source_layers, intersect_source_layers, difference_source_layers)
    metadata = {
        "stage3_region_source": FOREST_REGION_SOURCE,
        "forest_only": True,
        "forest_mask": {
            "source": mask_source,
            "source_region_code": source_region_code,
            "source_layers": source_layers,
            "intersect_source_layers": intersect_source_layers,
            "difference_source_layers": difference_source_layers,
            "source_qualities": [detail["source_quality"] for detail in source_details],
            "intersect_source_qualities": [detail["source_quality"] for detail in intersect_source_details],
            "difference_source_qualities": [detail["source_quality"] for detail in difference_source_details],
            "production_roles": [detail["production_role"] for detail in source_details],
            "intersect_production_roles": [detail["production_role"] for detail in intersect_source_details],
            "difference_production_roles": [detail["production_role"] for detail in difference_source_details],
            "method": forest_mask_method(
                mask_source=mask_source,
                has_intersect_mask=bool(intersect_source_layers),
                has_difference_mask=bool(difference_source_layers),
                clip_to_grid=bool(clip_to_grid_version),
            ),
        },
        "future_full_layer_a_source_adapter": True,
    }
    if min_mask_component_area_ha > 0:
        metadata["forest_mask"]["clean_mask_policy"] = {
            "method": "component_area_filter_before_grid_generation",
            "min_component_area_ha": float(min_mask_component_area_ha),
            "applies_before_grid_insert": True,
            "client_decision": "exclude_tiny_nonforest_or_fragmented_scraps_before_stage3_acceptance",
        }
    if clip_to_grid_version:
        metadata["forest_mask"]["clip_to_grid_version"] = clip_to_grid_version
    return metadata


def clean_mask_policy(metadata: dict[str, Any]) -> dict[str, Any]:
    forest_mask = normalize_region_metadata(metadata).get("forest_mask") or {}
    policy = forest_mask.get("clean_mask_policy") or {}
    return policy if isinstance(policy, dict) else {}


def clean_mask_min_component_area_ha(metadata: dict[str, Any]) -> float:
    policy = clean_mask_policy(metadata)
    try:
        value = float(policy.get("min_component_area_ha") or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def forest_mask_method(
    *,
    mask_source: str,
    has_intersect_mask: bool,
    has_difference_mask: bool,
    clip_to_grid: bool = False,
) -> str:
    if mask_source in {
        "public_osm_forest_mask",
        "public_osm_forest_mask_with_nonforest_difference",
    }:
        method = "public_osm_forest_mask_geometry"
    elif mask_source in {
        "public_ogm_layer_a_plus_osm_forest_mask",
        "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference",
    }:
        method = "public_ogm_layer_a_union_osm_forest_mask_geometry"
    elif has_intersect_mask:
        method = "dissolved_layer_a_forest_class_geometry_with_public_forest_mask"
    else:
        method = "dissolved_layer_a_forest_class_geometry"
    if clip_to_grid:
        method = f"{method}_clipped_to_grid"
    if has_difference_mask:
        method = f"{method}_and_nonforest_difference"
    return method


def forest_mask_source(
    source_layers: list[str],
    intersect_source_layers: list[str] | None = None,
    difference_source_layers: list[str] | None = None,
) -> str:
    intersect_source_layers = intersect_source_layers or []
    difference_source_layers = difference_source_layers or []
    source_details = [_source_detail(source_layer) for source_layer in source_layers]
    intersect_source_details = [_source_detail(source_layer) for source_layer in intersect_source_layers]
    difference_source_details = [_source_detail(source_layer) for source_layer in difference_source_layers]
    roles = {detail["production_role"] for detail in source_details}
    qualities = {detail["source_quality"] for detail in source_details}
    intersect_roles = {detail["production_role"] for detail in intersect_source_details}
    intersect_qualities = {detail["source_quality"] for detail in intersect_source_details}
    difference_roles = {detail["production_role"] for detail in difference_source_details}
    difference_qualities = {detail["source_quality"] for detail in difference_source_details}
    if (
        roles & PUBLIC_FOREST_MASK_ROLES
        and roles & AUXILIARY_FOREST_MASK_ROLES
        and roles <= (PUBLIC_FOREST_MASK_ROLES | AUXILIARY_FOREST_MASK_ROLES)
        and qualities <= (PUBLIC_FOREST_MASK_QUALITIES | AUXILIARY_FOREST_MASK_QUALITIES)
        and not intersect_roles
        and (
            not difference_roles
            or (
                difference_roles <= AUXILIARY_NONFOREST_EXCLUSION_ROLES
                and difference_qualities <= AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
            )
        )
    ):
        return (
            "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference"
            if difference_roles
            else "public_ogm_layer_a_plus_osm_forest_mask"
        )
    if (
        roles <= PUBLIC_FOREST_MASK_ROLES
        and qualities <= PUBLIC_FOREST_MASK_QUALITIES
        and intersect_roles
        and intersect_roles <= AUXILIARY_FOREST_MASK_ROLES
        and intersect_qualities <= AUXILIARY_FOREST_MASK_QUALITIES
        and (
            not difference_roles
            or (
                difference_roles <= AUXILIARY_NONFOREST_EXCLUSION_ROLES
                and difference_qualities <= AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
            )
        )
    ):
        return (
            "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference"
            if difference_roles
            else "public_ogm_layer_a_with_osm_forest_clip"
        )
    if roles <= PUBLIC_FOREST_MASK_ROLES and qualities <= PUBLIC_FOREST_MASK_QUALITIES:
        return "public_ogm_layer_a"
    if (
        roles <= AUXILIARY_FOREST_MASK_ROLES
        and qualities <= AUXILIARY_FOREST_MASK_QUALITIES
        and (
            not difference_roles
            or (
                difference_roles <= AUXILIARY_NONFOREST_EXCLUSION_ROLES
                and difference_qualities <= AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
            )
        )
    ):
        return "public_osm_forest_mask_with_nonforest_difference" if difference_roles else "public_osm_forest_mask"
    if roles <= FUTURE_FOREST_MASK_ROLES and qualities <= FUTURE_FOREST_MASK_QUALITIES:
        return "official_full_layer_a"
    return "mixed_layer_a"


def public_forest_mask_metadata_ready(metadata: dict[str, Any]) -> bool:
    forest_mask = metadata.get("forest_mask") or {}
    source = forest_mask.get("source")
    if source not in {
        "public_ogm_layer_a_plus_osm_forest_mask",
        "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference",
        "public_ogm_layer_a_with_osm_forest_clip",
        "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
        "public_osm_forest_mask",
        "public_osm_forest_mask_with_nonforest_difference",
    }:
        return False
    source_layers = forest_mask.get("source_layers") or []
    intersect_source_layers = forest_mask.get("intersect_source_layers") or []
    difference_source_layers = forest_mask.get("difference_source_layers") or []
    details = [_source_detail(source_layer) for source_layer in source_layers]
    intersect_details = [_source_detail(source_layer) for source_layer in intersect_source_layers]
    difference_details = [_source_detail(source_layer) for source_layer in difference_source_layers]
    source_qualities = set(forest_mask.get("source_qualities") or [detail["source_quality"] for detail in details])
    production_roles = set(forest_mask.get("production_roles") or [detail["production_role"] for detail in details])
    intersect_source_qualities = set(
        forest_mask.get("intersect_source_qualities") or [detail["source_quality"] for detail in intersect_details]
    )
    intersect_production_roles = set(
        forest_mask.get("intersect_production_roles") or [detail["production_role"] for detail in intersect_details]
    )
    difference_source_qualities = set(
        forest_mask.get("difference_source_qualities") or [detail["source_quality"] for detail in difference_details]
    )
    difference_production_roles = set(
        forest_mask.get("difference_production_roles") or [detail["production_role"] for detail in difference_details]
    )
    if source in {"public_osm_forest_mask", "public_osm_forest_mask_with_nonforest_difference"}:
        return (
            bool(source_layers)
            and source_qualities <= AUXILIARY_FOREST_MASK_QUALITIES
            and production_roles <= AUXILIARY_FOREST_MASK_ROLES
            and (
                not difference_source_layers
                or (
                    difference_source_qualities <= AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
                    and difference_production_roles <= AUXILIARY_NONFOREST_EXCLUSION_ROLES
                )
            )
        )
    if source in {
        "public_ogm_layer_a_plus_osm_forest_mask",
        "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference",
    }:
        return (
            bool(source_layers)
            and production_roles & PUBLIC_FOREST_MASK_ROLES
            and production_roles & AUXILIARY_FOREST_MASK_ROLES
            and production_roles <= (PUBLIC_FOREST_MASK_ROLES | AUXILIARY_FOREST_MASK_ROLES)
            and source_qualities <= (PUBLIC_FOREST_MASK_QUALITIES | AUXILIARY_FOREST_MASK_QUALITIES)
            and not intersect_source_layers
            and (
                not difference_source_layers
                or (
                    difference_source_qualities <= AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
                    and difference_production_roles <= AUXILIARY_NONFOREST_EXCLUSION_ROLES
                )
            )
        )
    return (
        bool(source_layers)
        and bool(intersect_source_layers)
        and source_qualities <= PUBLIC_FOREST_MASK_QUALITIES
        and production_roles <= PUBLIC_FOREST_MASK_ROLES
        and intersect_source_qualities <= AUXILIARY_FOREST_MASK_QUALITIES
        and intersect_production_roles <= AUXILIARY_FOREST_MASK_ROLES
        and (
            not difference_source_layers
            or (
                difference_source_qualities <= AUXILIARY_NONFOREST_EXCLUSION_QUALITIES
                and difference_production_roles <= AUXILIARY_NONFOREST_EXCLUSION_ROLES
            )
        )
    )


def manual_region_metadata(source: str) -> dict[str, Any]:
    return {
        "stage3_region_source": source,
        "forest_only": False,
        "usage": "seed_or_manual_region",
    }


def require_forest_only_region(
    db,
    *,
    region_code: str,
    allow_processing_extent: bool = False,
    mask_version: str | None = None,
    projected_srid: int | None = None,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT id, metadata
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).mappings().one()
    metadata = normalize_region_metadata(row.get("metadata"))
    if component_extent_info(metadata) is not None:
        if not allow_processing_extent or mask_version is None or projected_srid is None:
            raise RuntimeError("Processing extent requires explicit component-only grid validation")
        require_component_extent(
            db, region_id=int(row["id"]), metadata=metadata,
            mask_version=mask_version, projected_srid=projected_srid,
        )
    forest_mask_source = (metadata.get("forest_mask") or {}).get("source")
    allowed_sources = {
        "public_ogm_layer_a_plus_osm_forest_mask",
        "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference",
        "public_ogm_layer_a_with_osm_forest_clip",
        "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
        "public_osm_forest_mask",
        "public_osm_forest_mask_with_nonforest_difference",
        "official_full_layer_a",
    }
    if (
        metadata.get("forest_only") is True
        and metadata.get("stage3_region_source") in {FOREST_REGION_SOURCE, FILTERED_FOREST_REGION_SOURCE}
        and forest_mask_source in allowed_sources
    ):
        return metadata
    raise RuntimeError(
        "Stage 3 grid generation requires a forest-only region. "
        "Run run_stage3_prepare_region --from-layer-a first."
    )


def normalize_region_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def require_forest_geometry(metadata: Any) -> None:
    """Reject extent storage before a legacy consumer treats geom as forest."""
    if "region_geometry_role" in normalize_region_metadata(metadata):
        raise RuntimeError("Processing extent is not forest geometry; use authoritative components")


def component_extent_info(metadata: Any) -> dict[str, Any] | None:
    metadata = normalize_region_metadata(metadata)
    if "region_geometry_role" not in metadata:
        return None
    mask = metadata.get("forest_mask") or {}
    info = mask.get("component_extent") if isinstance(mask, dict) else None
    fragmented = mask.get("fragmented_component_mask") if isinstance(mask, dict) else None
    if (
        metadata["region_geometry_role"] != COMPONENT_EXTENT_ROLE
        or not isinstance(info, dict)
        or info.get("complete") is not True
        or not isinstance(info.get("mask_version"), str) or not info["mask_version"]
        or info["mask_version"] != mask.get("mask_version")
        or type(info.get("projected_srid")) is not int or info["projected_srid"] <= 0
        or type(info.get("fragment_count")) is not int or info["fragment_count"] <= 0
        or type(info.get("accepted_area_ha")) not in (int, float)
        or not math.isfinite(info["accepted_area_ha"]) or info["accepted_area_ha"] <= 0
        or type(info.get("first_component_id")) is not int or info["first_component_id"] <= 0
        or type(info.get("last_component_id")) is not int or info["last_component_id"] <= 0
        or not isinstance(info.get("processing_extent_code"), str) or not info["processing_extent_code"]
        or mask.get("component_storage") != "connected_component_fragments"
        or not isinstance(fragmented, dict)
        or fragmented.get("global_connected_component_filter") is not True
        or clean_mask_min_component_area_ha(metadata) != 1.0
    ):
        raise RuntimeError("Incomplete or incompatible component-authoritative processing extent")
    if info.get("publication_id") != _component_extent_publication_id(info):
        raise RuntimeError("Processing extent publication identity mismatch")
    return info


def _component_extent_publication_id(info: dict[str, Any]) -> str:
    fields = ("complete", "mask_version", "projected_srid", "fragment_count", "accepted_area_ha",
              "first_component_id", "last_component_id", "processing_extent_code")
    return hashlib.sha256(json.dumps({key: info[key] for key in fields}, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _matching_component_extent(metadata, mask_version, projected_srid):
    info = component_extent_info(metadata)
    if info is None:
        raise RuntimeError("Component-authoritative processing extent marker is required")
    if ((mask_version is not None and mask_version != info["mask_version"])
            or (projected_srid is not None and projected_srid != info["projected_srid"])):
        raise RuntimeError("Processing extent component mask version/SRID mismatch")
    return info


def _component_extent_stats(db, *, region_id, mask_version, projected_srid):
    return dict(db.execute(text("""
        WITH metrics AS (
        SELECT count(*) AS fragment_count,
               count(*) FILTER (WHERE geom_projected IS NULL OR geom_4326 IS NULL
                   OR ST_SRID(geom_projected) <> :projected_srid
                   OR ST_SRID(geom_4326) <> 4326) AS invalid_fragments,
               COALESCE(sum(ST_Area(geom_projected)) / 10000.0, 0) AS accepted_area_ha
        FROM stage3_region_mask_components
        WHERE region_id = :region_id AND mask_version = :mask_version
          AND projected_srid = :projected_srid
        )
        SELECT metrics.*,
            (SELECT id FROM stage3_region_mask_components
             WHERE region_id = :region_id AND mask_version = :mask_version
               AND projected_srid = :projected_srid AND component_index = 1) AS first_component_id,
            (SELECT id FROM stage3_region_mask_components
             WHERE region_id = :region_id AND mask_version = :mask_version
               AND projected_srid = :projected_srid
               AND component_index = metrics.fragment_count) AS last_component_id
        FROM metrics
    """), {"region_id": region_id, "mask_version": mask_version,
           "projected_srid": projected_srid}).mappings().one())


def component_extent_publication(db, *, region_id, mask_version, projected_srid,
                                 fragment_count, processing_extent_code):
    """Build the immutable descriptor inside the caller's publication transaction."""
    stats = _component_extent_stats(db, region_id=region_id, mask_version=mask_version,
                                    projected_srid=projected_srid)
    area = float(stats["accepted_area_ha"])
    if (fragment_count <= 0 or int(stats["fragment_count"]) != fragment_count
            or int(stats["invalid_fragments"]) != 0 or not math.isfinite(area) or area <= 0
            or stats["first_component_id"] is None or stats["last_component_id"] is None):
        raise RuntimeError("Processing extent authoritative components missing or incomplete")
    info = dict(complete=True, mask_version=mask_version, projected_srid=projected_srid,
                fragment_count=fragment_count, accepted_area_ha=area,
                first_component_id=int(stats["first_component_id"]),
                last_component_id=int(stats["last_component_id"]),
                processing_extent_code=processing_extent_code)
    info["publication_id"] = _component_extent_publication_id(info)
    return info


def require_component_extent(
    db, *, region_id: int, metadata: Any,
    mask_version: str | None = None, projected_srid: int | None = None,
) -> dict[str, Any]:
    """Full native scan for CLI preflight/audit only; never call from HTTP handlers."""
    info = _matching_component_extent(metadata, mask_version, projected_srid)
    stats = _component_extent_stats(db, region_id=region_id, mask_version=info["mask_version"],
                                   projected_srid=info["projected_srid"])
    area = float(stats["accepted_area_ha"])
    if (int(stats["fragment_count"]) != info["fragment_count"]
            or int(stats["invalid_fragments"]) != 0
            or stats["first_component_id"] != info["first_component_id"]
            or stats["last_component_id"] != info["last_component_id"]
            or not math.isfinite(area) or area <= 0):
        raise RuntimeError("Processing extent authoritative components missing or incomplete")
    return {**stats, "accepted_area_ha": area}


def require_component_extent_publication(
    db, *, region_id: int, metadata: Any,
    mask_version: str | None = None, projected_srid: int | None = None,
) -> dict[str, Any]:
    """HTTP check: immutable descriptor plus bounded unique-index probes, no geometry scan.

    Completion belongs to the atomic publication. Endpoint IDs detect replacement
    between metadata and tile reads. Full content checks remain CLI/native audits.
    """
    info = _matching_component_extent(metadata, mask_version, projected_srid)
    row = db.execute(text("""
        SELECT first_piece.id
        FROM stage3_region_mask_components first_piece
        JOIN stage3_region_mask_components last_piece
          ON last_piece.region_id = first_piece.region_id
         AND last_piece.mask_version = first_piece.mask_version
         AND last_piece.component_index = :fragment_count
        WHERE first_piece.region_id = :region_id AND first_piece.mask_version = :mask_version
          AND first_piece.component_index = 1
          AND first_piece.projected_srid = :projected_srid
          AND last_piece.projected_srid = :projected_srid
          AND first_piece.id = :first_component_id AND last_piece.id = :last_component_id
          AND NOT EXISTS (
              SELECT 1 FROM stage3_region_mask_components extra
              WHERE extra.region_id = :region_id AND extra.mask_version = :mask_version
                AND extra.component_index > :fragment_count LIMIT 1
          )
        LIMIT 1
    """), {"region_id": region_id, **info}).mappings().one_or_none()
    if row is None:
        raise RuntimeError("Processing extent authoritative components missing or publication changed")
    return info


def _source_detail(source_layer: str) -> dict[str, str]:
    definition = source_definition(source_layer)
    return {
        "source_quality": str(definition.get("source_quality") or "unknown"),
        "production_role": str(definition.get("production_role") or "unknown"),
    }
