from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

if TYPE_CHECKING:
    from app.core.config import Settings


PUBLIC_ARTIFACT_STATES = ("raster_cached", "zonal_done", "import_done", "validated")
PUBLIC_STAC_COG_SOURCE_ALGORITHM = "public-stac-cog-shared-source-tile-mosaic-v11"


@dataclass(frozen=True)
class Stage4PublicSourceAdapter:
    adapter: str
    display_name: str
    implemented: bool
    uses_account_auth: bool
    uses_batch_api: bool
    uses_commercial_storage: bool
    supports_shared_raster_cache: bool
    request_family: str


PUBLIC_SOURCE_ADAPTERS: dict[str, Stage4PublicSourceAdapter] = {
    "cdse_process": Stage4PublicSourceAdapter(
        adapter="cdse_process",
        display_name="CDSE Sentinel Hub Process API",
        implemented=True,
        uses_account_auth=True,
        uses_batch_api=False,
        uses_commercial_storage=False,
        supports_shared_raster_cache=True,
        request_family="process_raster",
    ),
    "cdse_odata_download": Stage4PublicSourceAdapter(
        adapter="cdse_odata_download",
        display_name="CDSE OData product download",
        implemented=False,
        uses_account_auth=True,
        uses_batch_api=False,
        uses_commercial_storage=False,
        supports_shared_raster_cache=True,
        request_family="product_download",
    ),
    "public_stac_cog": Stage4PublicSourceAdapter(
        adapter="public_stac_cog",
        display_name="Public STAC COG reader",
        implemented=True,
        uses_account_auth=False,
        uses_batch_api=False,
        uses_commercial_storage=False,
        supports_shared_raster_cache=True,
        request_family="public_cog",
    ),
}


def normalize_public_source_adapter(value: str) -> str:
    adapter = value.strip().lower()
    aliases = {
        "cdse": "cdse_process",
        "cdse-process": "cdse_process",
        "sentinel_hub_process": "cdse_process",
        "sentinel-hub-process": "cdse_process",
        "odata": "cdse_odata_download",
        "cdse-odata-download": "cdse_odata_download",
        "stac": "public_stac_cog",
        "public-stac-cog": "public_stac_cog",
    }
    adapter = aliases.get(adapter, adapter)
    if adapter not in PUBLIC_SOURCE_ADAPTERS:
        raise ValueError("Unsupported Stage 4 public source adapter")
    return adapter


def public_source_adapter_capabilities(settings: "Settings") -> Stage4PublicSourceAdapter:
    return PUBLIC_SOURCE_ADAPTERS[normalize_public_source_adapter(settings.stage4_public_source_adapter)]


def public_source_plan(settings: "Settings") -> dict[str, Any]:
    adapter = public_source_adapter_capabilities(settings)
    blockers = []
    if adapter.uses_batch_api:
        blockers.append("Public Stage 4 source adapter must not use Sentinel Hub Batch API")
    if adapter.uses_commercial_storage:
        blockers.append("Public Stage 4 source adapter must not use CloudFerro or commercial S3 storage")
    if adapter.uses_account_auth:
        blockers.append("Client-approved public Stage 4 source adapter must not require account authentication")
    if not adapter.implemented:
        blockers.append(
            f"Public source adapter {adapter.adapter} is registered but not implemented for production runs"
        )
    if adapter.supports_shared_raster_cache and not settings.stage4_public_raster_cache_enabled:
        blockers.append("Shared public raster cache is disabled")
    return {
        "adapter": adapter.adapter,
        "display_name": adapter.display_name,
        "implemented": adapter.implemented,
        "uses_account_auth": adapter.uses_account_auth,
        "uses_batch_api": adapter.uses_batch_api,
        "uses_commercial_storage": adapter.uses_commercial_storage,
        "supports_shared_raster_cache": adapter.supports_shared_raster_cache,
        "shared_raster_cache_enabled": bool(settings.stage4_public_raster_cache_enabled),
        "shared_raster_cache_dir": settings.stage4_public_raster_cache_dir,
        "artifact_states": list(PUBLIC_ARTIFACT_STATES),
        "blockers": blockers,
    }


def public_raster_cache_identity(
    *,
    run: dict[str, Any],
    batch: dict[str, Any],
    settings: "Settings",
    bbox: list[float],
    crs_epsg: int,
    width: int,
    height: int,
    target_mask_sha256: str | None = None,
    target_pixel_count: int | None = None,
    target_min_valid_pixel_ratio: float | None = None,
    target_mask_strategy: str | None = None,
    target_mask_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter = public_source_adapter_capabilities(settings)
    identity = {
        "source_adapter": adapter.adapter,
        "request_family": adapter.request_family,
        "data_collection": settings.stage4_public_stac_collection if adapter.adapter == "public_stac_cog" else "sentinel-2-l2a",
        "mosaicking_order": "leastCC",
        "requested_start": str(run["requested_start"]),
        "requested_end": str(run["requested_end"]),
        "max_cloud_coverage": int(run["max_cloud_coverage"]),
        "resolution_m": float(settings.stage4_public_raster_zonal_resolution_m),
        "bbox": [float(value) for value in bbox],
        "crs_epsg": int(crs_epsg),
        "width": int(width),
        "height": int(height),
        "evalscript_sha256": _ndvi_raster_evalscript_sha256(),
    }
    if adapter.adapter == "public_stac_cog":
        identity.update(
            {
                "stac_api_url": settings.stage4_public_stac_api_url,
                "stac_collection": settings.stage4_public_stac_collection,
                "stac_scene_limit": int(settings.stage4_public_stac_scene_limit),
                "source_algorithm": PUBLIC_STAC_COG_SOURCE_ALGORITHM,
                "source_tile_size_m": int(settings.stage4_public_raster_zonal_source_tile_size_m),
                "target_mask_strategy": target_mask_strategy or "full_raster",
                "target_mask_scope": target_mask_scope or {},
                "target_mask_sha256": target_mask_sha256 or "full-raster",
                "target_pixel_count": target_pixel_count,
                "target_min_valid_pixel_ratio": target_min_valid_pixel_ratio,
            }
        )
    return identity


def public_raster_cache_key(identity: dict[str, Any]) -> str:
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def public_raster_cache_paths(settings: "Settings", cache_key: str) -> tuple[Path, Path]:
    root = Path(settings.stage4_public_raster_cache_dir)
    raster_path = root / cache_key[:2] / f"{cache_key}.tif"
    manifest_path = raster_path.with_suffix(raster_path.suffix + ".manifest.json")
    return raster_path, manifest_path


def load_valid_public_raster_cache(settings: "Settings", identity: dict[str, Any]) -> Path | None:
    if not settings.stage4_public_raster_cache_enabled:
        return None
    cache_key = public_raster_cache_key(identity)
    raster_path, manifest_path = public_raster_cache_paths(settings, cache_key)
    if not raster_path.is_file() or not manifest_path.is_file() or raster_path.stat().st_size <= 0:
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("identity") != identity:
        return None
    if manifest.get("cache_key") != cache_key:
        return None
    if int(manifest.get("size_bytes") or 0) != raster_path.stat().st_size:
        return None
    expected_sha256 = manifest.get("sha256")
    if expected_sha256 and expected_sha256 != _file_sha256(raster_path):
        return None
    return raster_path


def write_public_raster_cache(
    settings: "Settings",
    identity: dict[str, Any],
    raster_bytes: bytes,
    validate: Callable[[Path], None],
    *,
    source_metadata: dict[str, Any] | None = None,
) -> Path:
    cache_key = public_raster_cache_key(identity)
    raster_path, manifest_path = public_raster_cache_paths(settings, cache_key)
    raster_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = raster_path.with_name(f"{raster_path.name}.{uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(raster_bytes)
        validate(tmp_path)
        tmp_path.replace(raster_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    payload = {
        "cache_key": cache_key,
        "identity": identity,
        "size_bytes": raster_path.stat().st_size,
        "sha256": _file_sha256(raster_path),
        "artifact_state": "raster_cached",
        "source_metadata": source_metadata or {},
    }
    manifest_tmp = manifest_path.with_name(f"{manifest_path.name}.{uuid4().hex}.tmp")
    manifest_tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    manifest_tmp.replace(manifest_path)
    return raster_path


def write_public_raster_cache_file(
    settings: "Settings",
    identity: dict[str, Any],
    source_path: Path,
    validate: Callable[[Path], None],
    *,
    source_metadata: dict[str, Any] | None = None,
) -> Path:
    cache_key = public_raster_cache_key(identity)
    raster_path, manifest_path = public_raster_cache_paths(settings, cache_key)
    raster_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = raster_path.with_name(f"{raster_path.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(source_path, tmp_path)
        validate(tmp_path)
        tmp_path.replace(raster_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    payload = {
        "cache_key": cache_key,
        "identity": identity,
        "size_bytes": raster_path.stat().st_size,
        "sha256": _file_sha256(raster_path),
        "artifact_state": "raster_cached",
        "source_metadata": source_metadata or {},
    }
    manifest_tmp = manifest_path.with_name(f"{manifest_path.name}.{uuid4().hex}.tmp")
    manifest_tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    manifest_tmp.replace(manifest_path)
    return raster_path


def read_public_raster_cache_source_metadata(
    settings: "Settings", identity: dict[str, Any]
) -> dict[str, Any]:
    # The caller validates the raster, identity and checksum before reading it.
    _, manifest_path = public_raster_cache_paths(settings, public_raster_cache_key(identity))
    payload = json.loads(manifest_path.read_text())
    source = payload.get("source_metadata")
    return dict(source) if isinstance(source, dict) else {}


def public_raster_cache_metadata(
    *,
    settings: "Settings",
    identity: dict[str, Any],
    raster_path: Path,
    cache_hit: bool,
) -> dict[str, Any]:
    cache_key = public_raster_cache_key(identity)
    return {
        "artifact_state": "raster_cached",
        "cache_key": cache_key,
        "cache_hit": bool(cache_hit),
        "source_adapter": identity["source_adapter"],
        "shared_cache": bool(settings.stage4_public_raster_cache_enabled),
        "bbox": identity["bbox"],
        "crs_epsg": identity["crs_epsg"],
        "width": identity["width"],
        "height": identity["height"],
        "requested_start": identity["requested_start"],
        "requested_end": identity["requested_end"],
        "size_bytes": raster_path.stat().st_size if raster_path.is_file() else 0,
    }


def _ndvi_raster_evalscript_sha256() -> str:
    from app.services.cdse import NDVI_RASTER_EVALSCRIPT

    return hashlib.sha256(NDVI_RASTER_EVALSCRIPT.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
