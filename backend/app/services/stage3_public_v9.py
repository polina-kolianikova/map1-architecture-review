from __future__ import annotations

import hashlib
import json
from typing import Any


STAGE3_PUBLIC_V9_REGION_CODE = "TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10"
STAGE3_PUBLIC_V9_GRID_VERSION = "stage3-public-ogm-osm-union-clean-v10"
STAGE3_PUBLIC_SOURCE_REGION_CODE = "TR-STAGE3-PUBLIC-FULL"
STAGE3_PUBLIC_V9_FORMULA = "public_ogm_union_osm_forest_minus_osm_nonforest"
STAGE3_PUBLIC_V9_SOURCE_LAYERS = ("ogm_esatis_bolme", "osm_forest_mask")
STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS = ()
STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS = ("osm_nonforest_exclusion",)
STAGE3_PUBLIC_V9_CACHE_SOURCE_LAYERS = (
    *STAGE3_PUBLIC_V9_SOURCE_LAYERS,
    *STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS,
    *STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS,
)
STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA = 1.0
STAGE3_PUBLIC_V9_GRID_SIZE_M = 100
STAGE3_PUBLIC_V9_TILE_SIZE_M = 10_000
STAGE3_PUBLIC_V9_PROJECTED_SRID = 6933
STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG = "--ack-full-turkey-user-approved"
STAGE3_PUBLIC_V9_FULL_SCOPE_MIN_AREA_HA = 1_000_000.0


def stage3_public_v9_manifest() -> dict[str, Any]:
    return {
        "manifest_version": "stage3_public_v10_client_formula_2026_08_13",
        "region_code": STAGE3_PUBLIC_V9_REGION_CODE,
        "grid_version": STAGE3_PUBLIC_V9_GRID_VERSION,
        "source_region_code": STAGE3_PUBLIC_SOURCE_REGION_CODE,
        "formula": STAGE3_PUBLIC_V9_FORMULA,
        "source_layers": list(STAGE3_PUBLIC_V9_SOURCE_LAYERS),
        "intersect_source_layers": list(STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS),
        "difference_source_layers": list(STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS),
        "cache_source_layers": list(STAGE3_PUBLIC_V9_CACHE_SOURCE_LAYERS),
        "cache_min_forest_component_area_ha": 0.0,
        "min_mask_component_area_ha": STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA,
        "grid_size_m": STAGE3_PUBLIC_V9_GRID_SIZE_M,
        "tile_size_m": STAGE3_PUBLIC_V9_TILE_SIZE_M,
        "projected_srid": STAGE3_PUBLIC_V9_PROJECTED_SRID,
        "full_scope_min_area_ha": STAGE3_PUBLIC_V9_FULL_SCOPE_MIN_AREA_HA,
        "public_data_only": True,
        "official_layer_a_status": "future_backlog",
    }


def stage3_public_v9_manifest_hash() -> str:
    payload = json.dumps(stage3_public_v9_manifest(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_stage3_public_v9_full_scope(
    *,
    region_code: str = "",
    grid_version: str = "",
    source_region_code: str = "",
    clip_region_code: str = "",
    scope_area_ha: float | None = None,
) -> bool:
    normalized_clip = clip_region_code or source_region_code
    return (
        region_code == STAGE3_PUBLIC_V9_REGION_CODE
        or grid_version == STAGE3_PUBLIC_V9_GRID_VERSION
        or (
            source_region_code == STAGE3_PUBLIC_SOURCE_REGION_CODE
            and normalized_clip == STAGE3_PUBLIC_SOURCE_REGION_CODE
        )
        or is_stage3_public_v9_national_scale_area(scope_area_ha)
    )


def is_stage3_public_v9_national_scale_area(scope_area_ha: float | None) -> bool:
    if scope_area_ha is None:
        return False
    try:
        return float(scope_area_ha) >= STAGE3_PUBLIC_V9_FULL_SCOPE_MIN_AREA_HA
    except (TypeError, ValueError):
        return False


def require_user_approved_full_turkey(
    *,
    approved: bool,
    region_code: str = "",
    grid_version: str = "",
    source_region_code: str = "",
    clip_region_code: str = "",
    scope_area_ha: float | None = None,
) -> None:
    if approved:
        return
    if not is_stage3_public_v9_full_scope(
        region_code=region_code,
        grid_version=grid_version,
        source_region_code=source_region_code,
        clip_region_code=clip_region_code,
        scope_area_ha=scope_area_ha,
    ):
        return
    raise RuntimeError(
        "Full Turkey V10 generation is locked until the user explicitly approves scaling. "
        f"Pass {STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG} only after that approval."
    )


def _as_tuple(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(values or ())


def _format_expected(actual: Any, expected: Any) -> str:
    return f"got {actual!r}, expected {expected!r}"


def require_stage3_public_v9_full_formula(
    *,
    region_code: str,
    source_region_code: str,
    clip_region_code: str,
    source_layers: list[str] | tuple[str, ...],
    intersect_source_layers: list[str] | tuple[str, ...],
    difference_source_layers: list[str] | tuple[str, ...],
    min_mask_component_area_ha: float,
    scope_area_ha: float | None = None,
) -> None:
    if not is_stage3_public_v9_full_scope(
        region_code=region_code,
        source_region_code=source_region_code,
        clip_region_code=clip_region_code,
        scope_area_ha=scope_area_ha,
    ):
        return

    problems: list[str] = []
    normalized_clip = clip_region_code or source_region_code
    if region_code != STAGE3_PUBLIC_V9_REGION_CODE:
        problems.append("region_code " + _format_expected(region_code, STAGE3_PUBLIC_V9_REGION_CODE))
    if source_region_code != STAGE3_PUBLIC_SOURCE_REGION_CODE:
        problems.append(
            "source_region_code " + _format_expected(source_region_code, STAGE3_PUBLIC_SOURCE_REGION_CODE)
        )
    if normalized_clip != STAGE3_PUBLIC_SOURCE_REGION_CODE:
        problems.append("clip_region_code " + _format_expected(normalized_clip, STAGE3_PUBLIC_SOURCE_REGION_CODE))
    if _as_tuple(source_layers) != STAGE3_PUBLIC_V9_SOURCE_LAYERS:
        problems.append("source_layers " + _format_expected(_as_tuple(source_layers), STAGE3_PUBLIC_V9_SOURCE_LAYERS))
    if _as_tuple(intersect_source_layers) != STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS:
        problems.append(
            "intersect_source_layers "
            + _format_expected(_as_tuple(intersect_source_layers), STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS)
        )
    if _as_tuple(difference_source_layers) != STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS:
        problems.append(
            "difference_source_layers "
            + _format_expected(_as_tuple(difference_source_layers), STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS)
        )
    if float(min_mask_component_area_ha) != STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA:
        problems.append(
            "min_mask_component_area_ha "
            + _format_expected(float(min_mask_component_area_ha), STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA)
        )
    if problems:
        raise RuntimeError(
            "Full Turkey V10 must use the exact client formula "
            "(ogm_esatis_bolme ∪ osm_forest_mask) − osm_nonforest_exclusion, "
            "then drop final connected components < 1 ha: "
            + "; ".join(problems)
        )


def require_stage3_public_v9_full_grid_args(
    *,
    region_code: str,
    grid_version: str,
    grid_size_m: int,
    projected_srid: int,
    tile_size_m: int,
    use_region_mask_components: bool,
    use_source_mask_cache: bool,
    grid_from_source_mask: bool,
    min_mask_component_area_ha: float,
    scope_area_ha: float | None = None,
) -> None:
    if not is_stage3_public_v9_full_scope(
        region_code=region_code,
        grid_version=grid_version,
        scope_area_ha=scope_area_ha,
    ):
        return

    problems: list[str] = []
    if region_code != STAGE3_PUBLIC_V9_REGION_CODE:
        problems.append("region_code " + _format_expected(region_code, STAGE3_PUBLIC_V9_REGION_CODE))
    if grid_version != STAGE3_PUBLIC_V9_GRID_VERSION:
        problems.append("grid_version " + _format_expected(grid_version, STAGE3_PUBLIC_V9_GRID_VERSION))
    if int(grid_size_m) != STAGE3_PUBLIC_V9_GRID_SIZE_M:
        problems.append("grid_size_m " + _format_expected(int(grid_size_m), STAGE3_PUBLIC_V9_GRID_SIZE_M))
    if int(projected_srid) != STAGE3_PUBLIC_V9_PROJECTED_SRID:
        problems.append("projected_srid " + _format_expected(int(projected_srid), STAGE3_PUBLIC_V9_PROJECTED_SRID))
    if int(tile_size_m) != STAGE3_PUBLIC_V9_TILE_SIZE_M:
        problems.append("tile_size_m " + _format_expected(int(tile_size_m), STAGE3_PUBLIC_V9_TILE_SIZE_M))
    if not use_region_mask_components:
        problems.append("use_region_mask_components must be true")
    if use_source_mask_cache:
        problems.append("use_source_mask_cache must be false for grid generation")
    if grid_from_source_mask:
        problems.append("grid_from_source_mask must be false for grid generation")
    if float(min_mask_component_area_ha) != STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA:
        problems.append(
            "min_mask_component_area_ha "
            + _format_expected(float(min_mask_component_area_ha), STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA)
        )
    if problems:
        raise RuntimeError(
            "Full Turkey V10 grid must be generated only from the finalized client-formula mask "
            "with exact 100 m / 1 ha grid settings: "
            + "; ".join(problems)
        )


def require_stage3_public_v9_full_source_cache_args(
    *,
    source_region_code: str,
    source_layers: list[str] | tuple[str, ...],
    projected_srid: int,
    min_forest_component_area_ha: float = 0.0,
    scope_area_ha: float | None = None,
) -> None:
    if not is_stage3_public_v9_full_scope(
        source_region_code=source_region_code,
        clip_region_code=source_region_code,
        scope_area_ha=scope_area_ha,
    ):
        return

    problems: list[str] = []
    if source_region_code != STAGE3_PUBLIC_SOURCE_REGION_CODE:
        problems.append(
            "source_region_code " + _format_expected(source_region_code, STAGE3_PUBLIC_SOURCE_REGION_CODE)
        )
    if _as_tuple(source_layers) != STAGE3_PUBLIC_V9_CACHE_SOURCE_LAYERS:
        problems.append(
            "source_layers "
            + _format_expected(_as_tuple(source_layers), STAGE3_PUBLIC_V9_CACHE_SOURCE_LAYERS)
        )
    if int(projected_srid) != STAGE3_PUBLIC_V9_PROJECTED_SRID:
        problems.append("projected_srid " + _format_expected(int(projected_srid), STAGE3_PUBLIC_V9_PROJECTED_SRID))
    if float(min_forest_component_area_ha) != 0.0:
        problems.append(
            "min_forest_component_area_ha "
            + _format_expected(float(min_forest_component_area_ha), 0.0)
        )
    if problems:
        raise RuntimeError(
            "Full Turkey V10 source cache must be prepared only for the exact client formula layers: "
            + "; ".join(problems)
        )


def stage3_public_v9_formula_manifest_matches(
    *,
    source_layers: list[str] | tuple[str, ...],
    intersect_source_layers: list[str] | tuple[str, ...],
    difference_source_layers: list[str] | tuple[str, ...],
    min_mask_component_area_ha: float,
    projected_srid: int,
) -> bool:
    return (
        _as_tuple(source_layers) == STAGE3_PUBLIC_V9_SOURCE_LAYERS
        and _as_tuple(intersect_source_layers) == STAGE3_PUBLIC_V9_INTERSECT_SOURCE_LAYERS
        and _as_tuple(difference_source_layers) == STAGE3_PUBLIC_V9_DIFFERENCE_SOURCE_LAYERS
        and float(min_mask_component_area_ha) == STAGE3_PUBLIC_V9_MIN_COMPONENT_AREA_HA
        and int(projected_srid) == STAGE3_PUBLIC_V9_PROJECTED_SRID
    )
