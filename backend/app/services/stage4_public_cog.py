from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from app.core.config import Settings


PUBLIC_STAC_ASSET_KEYS = ("red", "nir", "scl")
PUBLIC_STAC_SEARCH_LIMIT_MULTIPLIER = 4


class PublicStacAssetReadError(RuntimeError):
    pass


def build_public_stac_ndvi_raster(
    *,
    settings: "Settings",
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
    start: datetime,
    end: datetime,
    max_cloud_coverage: int,
    output_path: Path,
    target_mask: Any | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    scenes = search_public_stac_scenes(
        settings=settings,
        bbox=bbox,
        crs_epsg=crs_epsg,
        start=start,
        end=end,
        max_cloud_coverage=max_cloud_coverage,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if heartbeat is not None:
        heartbeat()
    if not scenes:
        _write_empty_ndvi_raster(output_path, bbox=bbox, crs_epsg=crs_epsg, width=width, height=height)
        return {
            "scene_count": 0,
            "selected_scene_ids": [],
            "selected_scene_datetimes": [],
            "selected_cloud_coverage": [],
            "collection": settings.stage4_public_stac_collection,
            "stac_api_url": settings.stage4_public_stac_api_url,
        }

    mosaic_stats = _write_ndvi_mosaic(
        output_path,
        scenes=scenes,
        bbox=bbox,
        crs_epsg=crs_epsg,
        width=width,
        height=height,
        http_timeout_seconds=int(settings.stage4_cdse_timeout_seconds),
        target_mask=target_mask,
        heartbeat=heartbeat,
    )
    return {
        "scene_count": len(scenes),
        "selected_scene_ids": [str(scene.get("id") or "") for scene in scenes],
        "selected_scene_datetimes": [str((scene.get("properties") or {}).get("datetime") or "") for scene in scenes],
        "selected_cloud_coverage": [
            (scene.get("properties") or {}).get("eo:cloud_cover")
            for scene in scenes
        ],
        "selected_bbox_overlap": [
            scene.get("_stage4_bbox_overlap")
            for scene in scenes
        ],
        "collection": settings.stage4_public_stac_collection,
        "stac_api_url": settings.stage4_public_stac_api_url,
        "mosaic": mosaic_stats,
    }


def check_public_stac_source(settings: "Settings") -> dict[str, Any]:
    response = _post_stac_search(
        settings,
        {
            "collections": [settings.stage4_public_stac_collection],
            "bbox": [29.0, 40.0, 29.01, 40.01],
            "datetime": "2026-05-01T00:00:00Z/2026-08-01T23:59:59Z",
            "limit": 1,
        },
    )
    payload = response.json()
    features = payload.get("features") or []
    return {
        "stac_status_code": response.status_code,
        "collection": settings.stage4_public_stac_collection,
        "stac_api_url": settings.stage4_public_stac_api_url,
        "sample_features": len(features),
    }


def search_public_stac_scenes(
    *,
    settings: "Settings",
    bbox: list[float],
    crs_epsg: int,
    start: datetime,
    end: datetime,
    max_cloud_coverage: int,
) -> list[dict[str, Any]]:
    query_bbox = _bbox_to_wgs84(bbox, crs_epsg)
    scene_limit = int(settings.stage4_public_stac_scene_limit)
    payload = {
        "collections": [settings.stage4_public_stac_collection],
        "bbox": query_bbox,
        "datetime": f"{_iso_z(start)}/{_iso_z(end)}",
        "limit": scene_limit * PUBLIC_STAC_SEARCH_LIMIT_MULTIPLIER,
        "query": {"eo:cloud_cover": {"lte": int(max_cloud_coverage)}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    response = _post_stac_search(settings, payload)
    features = response.json().get("features") or []
    scenes = [_require_cog_assets(feature) for feature in features if _has_cog_assets(feature)]
    return _rank_public_stac_scenes(scenes, query_bbox)[:scene_limit]


def _post_stac_search(settings: "Settings", payload: dict[str, Any]) -> requests.Response:
    response = requests.post(
        settings.stage4_public_stac_api_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=settings.stage4_cdse_timeout_seconds,
    )
    response.raise_for_status()
    return response


def _bbox_to_wgs84(bbox: list[float], crs_epsg: int) -> list[float]:
    if int(crs_epsg) == 4326:
        return [float(value) for value in bbox]
    try:
        from rasterio.warp import transform_bounds
    except ImportError as exc:  # pragma: no cover - checked by runtime readiness
        raise RuntimeError("Public STAC COG processing requires rasterio") from exc
    return [
        float(value)
        for value in transform_bounds(f"EPSG:{int(crs_epsg)}", "EPSG:4326", *bbox, densify_pts=21)
    ]


def _has_cog_assets(feature: dict[str, Any]) -> bool:
    assets = feature.get("assets") or {}
    return all(assets.get(key, {}).get("href") for key in PUBLIC_STAC_ASSET_KEYS)


def _require_cog_assets(feature: dict[str, Any]) -> dict[str, Any]:
    assets = feature.get("assets") or {}
    missing = [key for key in PUBLIC_STAC_ASSET_KEYS if not assets.get(key, {}).get("href")]
    if missing:
        raise RuntimeError("Public STAC scene is missing required Sentinel-2 COG assets")
    return feature


def _rank_public_stac_scenes(scenes: list[dict[str, Any]], query_bbox: list[float]) -> list[dict[str, Any]]:
    ranked = []
    for scene in scenes:
        overlap = _bbox_overlap_area(query_bbox, scene.get("bbox") or [])
        scene["_stage4_bbox_overlap"] = overlap
        ranked.append(scene)
    ranked.sort(
        key=lambda scene: (
            -float(scene.get("_stage4_bbox_overlap") or 0.0),
            _scene_cloud_coverage(scene),
            str((scene.get("properties") or {}).get("datetime") or ""),
        )
    )
    return ranked


def _scene_cloud_coverage(scene: dict[str, Any]) -> float:
    coverage = (scene.get("properties") or {}).get("eo:cloud_cover")
    return 100.0 if coverage is None else float(coverage)


def _bbox_overlap_area(left: list[float], right: list[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    min_x = max(float(left[0]), float(right[0]))
    min_y = max(float(left[1]), float(right[1]))
    max_x = min(float(left[2]), float(right[2]))
    max_y = min(float(left[3]), float(right[3]))
    if max_x <= min_x or max_y <= min_y:
        return 0.0
    return (max_x - min_x) * (max_y - min_y)


def _write_empty_ndvi_raster(
    output_path: Path,
    *,
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
) -> None:
    try:
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError as exc:  # pragma: no cover - checked by runtime readiness
        raise RuntimeError("Public STAC COG processing requires rasterio and numpy") from exc

    profile = _output_profile(bbox=bbox, crs_epsg=crs_epsg, width=width, height=height)
    with rasterio.open(output_path, "w", **profile) as dataset:
        dataset.write(np.zeros((height, width), dtype="float32"), 1)
        dataset.write(np.zeros((height, width), dtype="float32"), 2)


def _write_ndvi_mosaic(
    output_path: Path,
    *,
    scenes: list[dict[str, Any]],
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
    http_timeout_seconds: int,
    target_mask: Any | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds
        from rasterio.vrt import WarpedVRT
    except ImportError as exc:  # pragma: no cover - checked by runtime readiness
        raise RuntimeError("Public STAC COG processing requires rasterio and numpy") from exc

    transform = from_bounds(*bbox, width=width, height=height)
    dst_crs = f"EPSG:{int(crs_epsg)}"
    ndvi_out = np.zeros((height, width), dtype="float32")
    valid_out = np.zeros((height, width), dtype="float32")
    target_zones = None
    required_counts = None
    satisfied_zone_indexes = None
    if isinstance(target_mask, dict) and target_mask.get("zones") is not None:
        target_zones = np.asarray(target_mask["zones"], dtype="int32")
        if target_zones.shape != (height, width):
            raise RuntimeError("Public STAC target zone shape does not match the output raster")
        coverage_mask = target_zones > 0
        sample_counts = np.asarray(target_mask.get("sample_counts") or [], dtype="int64")
        required_counts = np.asarray(target_mask.get("required_counts") or [], dtype="int64")
        max_zone = int(target_zones.max()) if target_zones.size else 0
        if len(sample_counts) != len(required_counts) or len(required_counts) <= max_zone:
            raise RuntimeError("Public STAC target zone statistics do not match the output raster")
        valid_counts_by_zone = np.zeros(len(required_counts), dtype="int64")
        satisfied_zone_indexes = np.flatnonzero(required_counts[1:] > 0) + 1
        coverage_strategy = "cell_valid_ratio"
    elif target_mask is None:
        coverage_mask = np.ones((height, width), dtype=bool)
        coverage_strategy = "full_raster"
    else:
        coverage_mask = np.asarray(target_mask, dtype=bool)
        if coverage_mask.shape != (height, width):
            raise RuntimeError("Public STAC target mask shape does not match the output raster")
        coverage_strategy = "target_mask"
    target_pixel_count = int(coverage_mask.sum())

    rasterio_env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
        "AWS_NO_SIGN_REQUEST": "YES",
        "GDAL_HTTP_TIMEOUT": str(max(1, int(http_timeout_seconds))),
        "GDAL_HTTP_CONNECTTIMEOUT": "10",
        "GDAL_HTTP_MAX_RETRY": "1",
        "GDAL_HTTP_RETRY_DELAY": "1",
    }
    stats: dict[str, Any] = {
        "scenes_attempted": 0,
        "scenes_used": 0,
        "skipped_scene_count": 0,
        "skipped_scene_errors": [],
        "coverage_strategy": coverage_strategy,
        "target_pixel_count": target_pixel_count,
    }
    if satisfied_zone_indexes is not None:
        stats["target_cell_count"] = int(len(satisfied_zone_indexes))
        stats["target_min_valid_pixel_ratio"] = float(target_mask.get("min_valid_pixel_ratio") or 0)
    if target_pixel_count <= 0:
        _write_empty_ndvi_raster(output_path, bbox=bbox, crs_epsg=crs_epsg, width=width, height=height)
        stats["valid_pixel_count"] = 0
        stats["target_valid_pixel_count"] = 0
        stats["target_coverage_complete"] = False
        stats["target_satisfied_cell_count"] = 0
        stats["total_pixels"] = int(valid_out.size)
        return stats
    with rasterio.Env(**rasterio_env):
        for scene in scenes:
            stats["scenes_attempted"] += 1
            red = _read_asset_with_retry(scene, "red", dst_crs, transform, width, height, Resampling.bilinear, heartbeat=heartbeat)
            nir = _read_asset_with_retry(scene, "nir", dst_crs, transform, width, height, Resampling.bilinear, heartbeat=heartbeat)
            scl = _read_asset_with_retry(scene, "scl", dst_crs, transform, width, height, Resampling.nearest, heartbeat=heartbeat)
            red_reflectance = _apply_scale_offset(red["array"], scene, "red")
            nir_reflectance = _apply_scale_offset(nir["array"], scene, "nir")
            denom = nir_reflectance + red_reflectance
            valid_class = (scl["array"] == 4) | (scl["array"] == 5) | (scl["array"] == 6)
            valid_pixel = (
                valid_class
                & red["valid"]
                & nir["valid"]
                & scl["valid"]
                & np.isfinite(denom)
                & (denom != 0)
            )
            fill = valid_pixel & coverage_mask & (valid_out <= 0.5)
            if target_zones is not None and required_counts is not None:
                unfinished_zones = (required_counts > 0) & (valid_counts_by_zone < required_counts)
                fill &= unfinished_zones[target_zones]
            if not fill.any():
                if heartbeat is not None:
                    heartbeat()
                continue
            ndvi_out[fill] = (
                (nir_reflectance[fill] - red_reflectance[fill]) / denom[fill]
            ).astype("float32")
            valid_out[fill] = 1.0
            if target_zones is not None and required_counts is not None:
                filled_zones = target_zones[fill]
                filled_zones = filled_zones[filled_zones > 0]
                if filled_zones.size:
                    valid_counts_by_zone += np.bincount(filled_zones, minlength=len(required_counts))
            stats["scenes_used"] += 1
            if (
                satisfied_zone_indexes is not None
                and bool(
                    (
                        valid_counts_by_zone[satisfied_zone_indexes]
                        >= required_counts[satisfied_zone_indexes]
                    ).all()
                )
            ):
                if heartbeat is not None:
                    heartbeat()
                break
            if satisfied_zone_indexes is None and bool((valid_out[coverage_mask] > 0.5).all()):
                if heartbeat is not None:
                    heartbeat()
                break
            if heartbeat is not None:
                heartbeat()

    if (
        stats["scenes_attempted"] > 0
        and stats["skipped_scene_count"] == stats["scenes_attempted"]
        and stats["scenes_used"] == 0
    ):
        raise RuntimeError("Public STAC COG scenes were found but none could be read")

    profile = _output_profile(bbox=bbox, crs_epsg=crs_epsg, width=width, height=height)
    with rasterio.open(output_path, "w", **profile) as dataset:
        dataset.write(ndvi_out, 1)
        dataset.write(valid_out, 2)
    stats["valid_pixel_count"] = int((valid_out > 0.5).sum())
    stats["target_valid_pixel_count"] = int((valid_out[coverage_mask] > 0.5).sum())
    if satisfied_zone_indexes is not None and required_counts is not None:
        stats["target_satisfied_cell_count"] = int(
            (
                valid_counts_by_zone[satisfied_zone_indexes]
                >= required_counts[satisfied_zone_indexes]
            ).sum()
        )
        stats["target_coverage_complete"] = stats["target_satisfied_cell_count"] == int(len(satisfied_zone_indexes))
    else:
        stats["target_coverage_complete"] = stats["target_valid_pixel_count"] == target_pixel_count
    stats["total_pixels"] = int(valid_out.size)
    return stats


def _read_asset_with_retry(*args, heartbeat: Callable[[], None] | None = None):
    for attempt in range(3):
        try:
            return _read_asset(*args)
        except (OSError, requests.RequestException) as exc:
            if heartbeat is not None:
                heartbeat()
            if attempt == 2:
                raise PublicStacAssetReadError("Public STAC COG asset could not be read; retry the batch") from exc
            time.sleep(attempt + 1)


def _read_asset(
    scene: dict[str, Any],
    asset_key: str,
    dst_crs: str,
    transform: Any,
    width: int,
    height: int,
    resampling: Any,
) -> dict[str, Any]:
    import numpy as np
    import rasterio
    from rasterio.vrt import WarpedVRT

    asset = (scene.get("assets") or {})[asset_key]
    href = asset["href"]
    with rasterio.open(href) as source:
        src_nodata = source.nodata
        vrt_options = {
            "crs": dst_crs,
            "transform": transform,
            "width": width,
            "height": height,
            "resampling": resampling,
            "dst_nodata": 0,
        }
        if src_nodata is not None:
            vrt_options["src_nodata"] = src_nodata
        with WarpedVRT(source, **vrt_options) as vrt:
            array = vrt.read(1, masked=False)
            valid = np.ones((height, width), dtype=bool)
            if src_nodata is not None:
                valid &= array != src_nodata
            valid &= np.isfinite(array)
    return {"array": array, "valid": valid}


def _apply_scale_offset(array: Any, scene: dict[str, Any], asset_key: str) -> Any:
    import numpy as np

    asset = (scene.get("assets") or {})[asset_key]
    raster_bands = asset.get("raster:bands") or []
    band = raster_bands[0] if raster_bands else {}
    scale = float(band.get("scale") if band.get("scale") is not None else 1.0)
    offset = float(band.get("offset") if band.get("offset") is not None else 0.0)
    if (
        scene.get("collection") == "sentinel-2-l2a"
        and (scene.get("properties") or {}).get("earthsearch:boa_offset_applied") is True
        and asset_key in {"red", "nir"}
    ):
        # Legacy Earth Search COGs with this explicit flag already include the
        # BOA offset, although raster:bands can still advertise it. Collection
        # 1 and unknown flags retain the declared scale/offset without guessing.
        offset = 0.0
    values = array.astype("float32") * scale + offset
    return np.maximum(values, 0.0)


def _output_profile(*, bbox: list[float], crs_epsg: int, width: int, height: int) -> dict[str, Any]:
    from rasterio.transform import from_bounds

    return {
        "driver": "GTiff",
        "height": int(height),
        "width": int(width),
        "count": 2,
        "dtype": "float32",
        "crs": f"EPSG:{int(crs_epsg)}",
        "transform": from_bounds(*bbox, width=int(width), height=int(height)),
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }


def _iso_z(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
