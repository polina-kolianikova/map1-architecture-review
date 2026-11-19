from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from app.services.cdse import CdseClient
from app.services.stage4_public_sources import public_source_adapter_capabilities, public_source_plan

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True)
class Stage4ProviderCapabilities:
    adapter: str
    processing_path: str
    ndvi_processing_mode: str
    uses_external_jobs: bool
    uses_object_storage_exchange: bool
    diagnostic_only: bool
    supports_full_scope: bool
    batching_strategy: str
    public_source_adapter: str = ""
    uses_account_auth: bool = False
    uses_batch_api: bool = False
    uses_commercial_storage: bool = False
    shared_raster_cache_supported: bool = False
    artifact_states: tuple[str, ...] = ()


class Stage4NdviProvider(Protocol):
    capabilities: Stage4ProviderCapabilities

    def process_batch(self, run: dict[str, Any], batch: dict[str, Any], settings: "Settings") -> Any:
        ...

    def check_permissions(self, settings: "Settings") -> dict[str, Any]:
        ...


class CdsePublicStatisticsProvider:
    capabilities = Stage4ProviderCapabilities(
        adapter="cdse_public_statistics",
        processing_path="public",
        ndvi_processing_mode="public_statistics",
        uses_external_jobs=False,
        uses_object_storage_exchange=False,
        diagnostic_only=True,
        supports_full_scope=False,
        batching_strategy="cell_range",
    )

    def process_batch(self, run: dict[str, Any], batch: dict[str, Any], settings: "Settings") -> Any:
        from app.services import stage4_automation

        return stage4_automation.process_public_statistics_ndvi_batch(run, batch, settings)

    def check_permissions(self, settings: "Settings") -> dict[str, Any]:
        try:
            client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
            token = client.get_access_token()
            catalogue_response = client.session.get(
                f"{settings.cdse_catalogue_url}/Products?$top=1",
                headers={"Authorization": f"Bearer {token}"},
                timeout=settings.stage4_cdse_timeout_seconds,
            )
            statistics_end = datetime.now(UTC)
            statistics_start = statistics_end - timedelta(days=30)
            statistics_response = client.check_ndvi_statistics_endpoint(
                token,
                start=statistics_start,
                end=statistics_end,
                max_cloud_coverage=80,
                resolution=settings.stage4_statistics_resolution,
            )
        except Exception as exc:  # pragma: no cover - network-only safety gate
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "blocker": "CDSE public statistics permission check failed",
                "error": type(exc).__name__,
            }
        if catalogue_response.status_code >= 400:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "catalogue_status_code": catalogue_response.status_code,
                "blocker": "CDSE public catalogue endpoint is not ready",
            }
        if statistics_response.status_code >= 400:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "catalogue_status_code": catalogue_response.status_code,
                "statistics_status_code": statistics_response.status_code,
                "blocker": "CDSE public statistics endpoint is not ready",
            }
        return {
            "checked": True,
            "ok": True,
            "provider_adapter": self.capabilities.adapter,
            "catalogue_status_code": catalogue_response.status_code,
            "statistics_status_code": statistics_response.status_code,
        }


class PublicRasterZonalProvider:
    capabilities = Stage4ProviderCapabilities(
        adapter="public_raster_zonal",
        processing_path="public",
        ndvi_processing_mode="public_raster_zonal",
        uses_external_jobs=False,
        uses_object_storage_exchange=False,
        diagnostic_only=False,
        supports_full_scope=True,
        batching_strategy="stage3_spatial_tiles",
        public_source_adapter="public_stac_cog",
        uses_account_auth=False,
        uses_batch_api=False,
        uses_commercial_storage=False,
        shared_raster_cache_supported=True,
        artifact_states=("raster_cached", "zonal_done", "import_done", "validated"),
    )

    def process_batch(self, run: dict[str, Any], batch: dict[str, Any], settings: "Settings") -> Any:
        from app.services import stage4_automation

        return stage4_automation.process_public_raster_zonal_ndvi_batch(run, batch, settings)

    def check_permissions(self, settings: "Settings") -> dict[str, Any]:
        source_plan = public_source_plan(settings)
        source_adapter = public_source_adapter_capabilities(settings)
        if source_plan["blockers"] and not (
            source_adapter.adapter == "cdse_process"
            and source_plan["blockers"] == ["Shared public raster cache is disabled"]
        ):
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "source_adapter": source_adapter.adapter,
                "blocker": "; ".join(source_plan["blockers"]),
                "public_source_plan": source_plan,
            }
        missing_tools = [tool for tool in ("ogr2ogr", "gdalinfo") if shutil.which(tool) is None]
        missing_python = [
            package
            for package in ("numpy", "rasterio")
            if importlib.util.find_spec(package) is None
        ]
        dir_report = _public_raster_zonal_dir_report(settings)
        dir_blocker = next((item for item in dir_report if not item["ok"]), None)
        if missing_tools:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "blocker": "Public raster-zonal GDAL tools are missing",
                "missing_tools": missing_tools,
            }
        if missing_python:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "blocker": "Public raster-zonal Python packages are missing",
                "missing_python_packages": missing_python,
            }
        if dir_blocker:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "blocker": dir_blocker["blocker"],
                "directories": dir_report,
            }
        try:
            if source_adapter.adapter == "cdse_process":
                client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
                token = client.get_access_token()
                catalogue_response = client.session.get(
                    f"{settings.cdse_catalogue_url}/Products?$top=1",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=settings.stage4_cdse_timeout_seconds,
                )
                process_response = client.request_ndvi_raster(
                    token,
                    start=datetime.now(UTC) - timedelta(days=90),
                    end=datetime.now(UTC),
                    bbox=[29.0, 40.0, 29.01, 40.01],
                    crs_epsg=4326,
                    width=1,
                    height=1,
                    max_cloud_coverage=100,
                )
                if catalogue_response.status_code >= 400:
                    return {
                        "checked": True,
                        "ok": False,
                        "provider_adapter": self.capabilities.adapter,
                        "catalogue_status_code": catalogue_response.status_code,
                        "blocker": "CDSE public catalogue endpoint is not ready",
                    }
                source_probe = {
                    "catalogue_status_code": catalogue_response.status_code,
                    "process_probe_bytes": len(process_response),
                }
            elif source_adapter.adapter == "public_stac_cog":
                from app.services.stage4_public_cog import check_public_stac_source

                source_probe = check_public_stac_source(settings)
            else:
                return {
                    "checked": True,
                    "ok": False,
                    "provider_adapter": self.capabilities.adapter,
                    "source_adapter": source_adapter.adapter,
                    "blocker": f"Public source adapter {source_adapter.adapter} is not implemented for raster-zonal processing",
                    "public_source_plan": source_plan,
                }
        except Exception as exc:  # pragma: no cover - network-only safety gate
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "source_adapter": source_adapter.adapter,
                "blocker": "Public raster-zonal source permission check failed",
                "error": type(exc).__name__,
            }
        return {
            "checked": True,
            "ok": True,
            "provider_adapter": self.capabilities.adapter,
            "source_adapter": source_adapter.adapter,
            **source_probe,
            "required_tools": ["ogr2ogr", "gdalinfo"],
            "required_python_packages": ["numpy", "rasterio"],
            "directories": dir_report,
            "public_source_plan": source_plan,
        }


class IndustrialBatchStatisticalProvider:
    capabilities = Stage4ProviderCapabilities(
        adapter="industrial_batch_statistical",
        processing_path="industrial",
        ndvi_processing_mode="batch_statistical",
        uses_external_jobs=True,
        uses_object_storage_exchange=True,
        diagnostic_only=False,
        supports_full_scope=True,
        batching_strategy="cell_range",
    )

    def process_batch(self, run: dict[str, Any], batch: dict[str, Any], settings: "Settings") -> dict[str, Any]:
        from app.services import stage4_automation

        if batch["status"] == "external_ready":
            return stage4_automation.import_batch_statistical_ndvi_batch(run, batch, settings)
        return stage4_automation.submit_batch_statistical_ndvi_batch(run, batch, settings)

    def check_permissions(self, settings: "Settings") -> dict[str, Any]:
        try:
            client = CdseClient(settings, timeout=settings.stage4_cdse_timeout_seconds)
            token = client.get_access_token()
            response = client.session.get(
                settings.sentinel_hub_batch_statistics_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=settings.stage4_cdse_timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - network-only safety gate
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "blocker": "Batch Statistical provider permission check failed",
                "error": type(exc).__name__,
            }

        error_code = None
        if response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    error_code = error.get("code")

        if response.status_code == 403:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "status_code": response.status_code,
                "error_code": error_code,
                "blocker": "CDSE account is not authorized for Sentinel Hub Batch Statistical API",
            }
        if response.status_code >= 400:
            return {
                "checked": True,
                "ok": False,
                "provider_adapter": self.capabilities.adapter,
                "status_code": response.status_code,
                "error_code": error_code,
                "blocker": "Batch Statistical provider endpoint is not ready",
            }
        return {
            "checked": True,
            "ok": True,
            "provider_adapter": self.capabilities.adapter,
            "status_code": response.status_code,
        }


def _public_raster_zonal_dir_report(settings: "Settings") -> list[dict[str, Any]]:
    report = []
    min_free_bytes = int(float(settings.stage4_public_raster_zonal_min_free_gb) * 1024 * 1024 * 1024)
    for label, raw_path in (
        ("results", settings.stage4_public_raster_zonal_results_dir),
        ("work", settings.stage4_work_dir),
        *(
            (("shared_raster_cache", settings.stage4_public_raster_cache_dir),)
            if settings.stage4_public_raster_cache_enabled
            else ()
        ),
    ):
        path = Path(raw_path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".stage4-write-check"
            probe.write_text("ok\n")
            probe.unlink(missing_ok=True)
            usage = shutil.disk_usage(path)
        except OSError as exc:
            report.append(
                {
                    "name": label,
                    "path": str(path),
                    "ok": False,
                    "blocker": f"Public raster-zonal {label} directory is not writable",
                    "error": type(exc).__name__,
                }
            )
            continue
        free_bytes = int(usage.free)
        report.append(
            {
                "name": label,
                "path": str(path),
                "ok": free_bytes >= min_free_bytes,
                "free_gb": round(free_bytes / 1024 / 1024 / 1024, 2),
                "min_free_gb": float(settings.stage4_public_raster_zonal_min_free_gb),
                "blocker": ""
                if free_bytes >= min_free_bytes
                else f"Public raster-zonal {label} directory does not have enough free space",
            }
        )
    return report


def get_stage4_ndvi_provider(settings: "Settings") -> Stage4NdviProvider:
    adapter = _normalized_adapter(settings)
    if adapter == "cdse_public_statistics":
        return CdsePublicStatisticsProvider()
    if adapter == "public_raster_zonal":
        return PublicRasterZonalProvider()
    if adapter == "industrial_batch_statistical":
        return IndustrialBatchStatisticalProvider()
    raise RuntimeError(f"Unsupported Stage 4 provider adapter: {adapter}")


def stage4_provider_capabilities(settings: "Settings") -> Stage4ProviderCapabilities:
    capabilities = get_stage4_ndvi_provider(settings).capabilities
    if capabilities.adapter != "public_raster_zonal":
        return capabilities
    source_adapter = public_source_adapter_capabilities(settings)
    return replace(
        capabilities,
        public_source_adapter=source_adapter.adapter,
        uses_account_auth=source_adapter.uses_account_auth,
        uses_batch_api=source_adapter.uses_batch_api,
        uses_commercial_storage=source_adapter.uses_commercial_storage,
        shared_raster_cache_supported=source_adapter.supports_shared_raster_cache,
    )


def _normalized_adapter(settings: "Settings") -> str:
    adapter = settings.stage4_provider_adapter.strip().lower()
    if adapter == "auto":
        if settings.stage4_ndvi_processing_mode.strip().lower() == "public_raster_zonal":
            return "public_raster_zonal"
        return "industrial_batch_statistical" if settings.stage4_processing_path == "industrial" else "cdse_public_statistics"
    public_aliases = {"public", "public_statistics", "cell_statistics", "cdse_public_statistics"}
    public_raster_aliases = {"public_raster_zonal", "raster_zonal"}
    industrial_aliases = {"industrial", "batch_statistical", "industrial_batch_statistical", "sentinel_hub_batch_statistical"}
    if adapter in public_aliases:
        return "cdse_public_statistics"
    if adapter in public_raster_aliases:
        return "public_raster_zonal"
    if adapter in industrial_aliases:
        return "industrial_batch_statistical"
    raise RuntimeError(f"Unsupported Stage 4 provider adapter: {adapter}")
