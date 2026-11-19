from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret_key: str = Field(default="", repr=False)
    app_default_locale: str = "en"
    app_supported_locales: str = "en"
    backend_basic_auth_user: str = ""
    backend_basic_auth_password: str = Field(default="", repr=False)
    trust_local_reverse_proxy_only: bool = False
    cors_allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "map1_turkey"
    postgres_user: str = "map1"
    postgres_password: str = Field(default="", repr=False)
    postgres_app_user: str = "map1_app"
    postgres_app_password: str = Field(default="", repr=False)
    stage5_rules_write_token: str = Field(default="", repr=False)

    cdse_client_id: str = ""
    cdse_client_secret: str = Field(default="", repr=False)
    cdse_username: str = ""
    cdse_password: str = Field(default="", repr=False)
    cdse_auth_grant_type: str = "auto"
    cdse_totp: str = Field(default="", repr=False)
    cdse_proxy_url: str = Field(default="", repr=False)
    cdse_auth_url: str = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
        "protocol/openid-connect/token"
    )
    cdse_catalogue_url: str = "https://catalogue.dataspace.copernicus.eu/odata/v1"
    sentinel_hub_statistics_url: str = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
    sentinel_hub_batch_statistics_url: str = "https://sh.dataspace.copernicus.eu/api/v1/statistics/batch"
    sentinel_hub_process_url: str = "https://sh.dataspace.copernicus.eu/api/v1/process"

    ogm_base_url: str = "https://www.ogm.gov.tr"
    ogm_eharita_url: str = "https://www.ogm.gov.tr/tr/ormanlarimiz/e-harita"
    ogm_portal_url: str = "https://cbsportal.ogm.gov.tr/ogmportal"
    ogm_webmap_item_id: str = "1d210d5cd6cd42ecb7431e28995a0bc8"
    ogm_arazi_ortusu_wms_url: str = "https://wsapi.ogm.gov.tr/apigateway/ARAZI_ORTUSU_WMS"
    ogm_mescere_wms_url: str = "https://wsapi.ogm.gov.tr/apigateway/YAYIN_MESCERE_WMS"
    ogm_seflik_wms_url: str = "https://wsapi.ogm.gov.tr/apigateway/YAYIN_SEFLIK_WMS"
    ogm_isletme_wms_url: str = "https://wsapi.ogm.gov.tr/apigateway/YAYIN_ISLETME_WMS"
    ogm_bolge_wms_url: str = "https://wsapi.ogm.gov.tr/apigateway/INT_BOLGE_WMS"
    ogm_indeks_wms_url: str = "https://wsapi.ogm.gov.tr/apigateway/YAYIN_INDEKS_WMS"
    ogm_bolme_feature_url: str = "https://wsapi.ogm.gov.tr/apigateway/ESATIS_BOLME/0"
    ogm_tls_verify: bool = True
    ogm_proxy_url: str = Field(default="", repr=False)
    tucbs_orman_wms_url: str = "https://tucbs-public-api.csb.gov.tr/ormansu_orman"
    active_region_code: str = "REVIEW-SCOPE"
    active_grid_version: str = "review-grid"
    stage1_layer_a_source_layers: str = "ogm_esatis_bolme,ogm_yayin_mescere_wms_svg"
    stage1_verification_source_layers: str = "ogm_esatis_bolme,ogm_yayin_mescere_wms_svg"
    stage3_forest_mask_source_layers: str = "ogm_esatis_bolme,ogm_yayin_mescere_wms_svg"
    stage3_v10_partitioned_grid_reads: bool = False
    stage3_partitioned_evidence_reads: bool = False
    stage4_queue_url: str = "redis://redis:6379/0"
    stage4_result_backend_url: str = "redis://redis:6379/1"
    stage4_region_code: str = "REVIEW-SCOPE"
    stage4_grid_version: str = "review-grid"
    stage4_scheduler_enabled: bool = False
    stage4_update_cadence_days: int = 10
    stage4_batch_size: int = 500_000
    stage4_processing_path: str = "public"
    stage4_provider_adapter: str = "public_raster_zonal"
    stage4_ndvi_processing_mode: str = "public_raster_zonal"
    stage4_public_proof_max_cells: int = 10
    stage4_public_statistics_max_cells_per_batch: int = 10
    stage4_public_statistics_max_cells_per_run: int = 50
    stage4_public_statistics_allow_full_scope: bool = False
    stage4_public_statistics_hard_ceiling_cells: int = 100_000
    stage4_public_raster_zonal_max_cells_per_batch: int = 50_000
    stage4_public_raster_zonal_resolution_m: float = 10.0
    stage4_public_raster_zonal_source_tile_size_m: int = 50_000
    stage4_public_raster_zonal_max_pixels: int = 25_000_000
    stage4_public_raster_zonal_min_free_gb: float = 5.0
    stage4_public_raster_zonal_reuse_existing_csv: bool = False
    stage4_public_raster_zonal_partitioned_mirror_enabled: bool = False
    stage4_public_raster_zonal_results_dir: str = "/app/artifacts/stage4/public-raster-zonal"
    stage4_public_source_adapter: str = "public_stac_cog"
    stage4_public_raster_cache_enabled: bool = True
    stage4_public_raster_cache_dir: str = "/app/artifacts/stage4/public-raster-cache"
    stage4_public_stac_api_url: str = "https://earth-search.aws.element84.com/v1/search"
    stage4_public_stac_collection: str = "sentinel-2-l2a"
    stage4_public_stac_scene_limit: int = 24
    stage4_batch_statistical_input_uri: str = ""
    stage4_batch_statistical_output_uri: str = ""
    stage4_batch_statistical_s3_iam_role_arn: str = Field(default="", repr=False)
    stage4_batch_statistical_s3_access_key: str = Field(default="", repr=False)
    stage4_batch_statistical_s3_secret_access_key: str = Field(default="", repr=False)
    stage4_batch_statistical_s3_region: str = "WAW3-1"
    stage4_batch_statistical_s3_endpoint_url: str = ""
    stage4_batch_statistical_max_features: int = 700_000
    stage4_work_dir: str = "/app/artifacts/stage4"
    stage4_cell_statistics_max_cells: int = 100_000
    stage4_max_cloud_coverage: int = 40
    stage4_min_valid_pixel_ratio: float = 0.35
    stage4_statistics_resolution: float = 0.0001
    stage4_request_delay_seconds: float = 0.0
    stage4_cdse_timeout_seconds: int = 60
    stage4_allow_source_unavailable: bool = False
    stage4_source_unavailable_circuit_cooldown_minutes: int = 15
    stage4_source_unavailable_retry_seconds: int = 900
    stage4_provider_max_concurrency: int = 1
    stage4_task_soft_time_limit_seconds: int = 3 * 60 * 60
    stage4_task_time_limit_seconds: int = 4 * 60 * 60
    stage4_ndvi_update_enabled: bool = True
    stage4_biomass_update_enabled: bool = True
    stage4_biomass_formula_enabled: bool = False
    stage4_biomass_formula_version: str = "pending-client-calibration"
    stage4_biomass_ndvi_slope: float = 0.0
    stage4_biomass_ndvi_intercept: float = 0.0
    stage4_biomass_unit: str = "t_dry_matter_per_ha"
    stage4_history_storage_uri: str = ""
    stage4_stuck_after_minutes: int = 120
    stage4_alert_cooldown_minutes: int = 60

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def normalize_string_env(cls, value):
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in ("'", '"'):
            return normalized[1:-1]
        return normalized

    @field_validator("stage4_update_cadence_days")
    @classmethod
    def validate_stage4_update_cadence_days(cls, value: int) -> int:
        if value < 5 or value > 10:
            raise ValueError("Stage 4 update cadence must be between 5 and 10 days")
        return value

    @field_validator("stage4_batch_size")
    @classmethod
    def validate_stage4_batch_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Stage 4 batch size must be greater than zero")
        return value

    @field_validator("stage4_ndvi_processing_mode")
    @classmethod
    def validate_stage4_ndvi_processing_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {
            "public_statistics",
            "public_raster_zonal",
            "batch_statistical",
            "cell_statistics",
        }:
            raise ValueError(
                "Stage 4 NDVI processing mode must be public_statistics, "
                "public_raster_zonal, batch_statistical, or cell_statistics"
            )
        return normalized

    @field_validator("stage4_processing_path")
    @classmethod
    def validate_stage4_processing_path(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"public", "industrial"}:
            raise ValueError("Stage 4 processing path must be public or industrial")
        return normalized

    @field_validator("stage4_provider_adapter")
    @classmethod
    def validate_stage4_provider_adapter(cls, value: str) -> str:
        normalized = value.strip().lower()
        aliases = {
            "auto",
            "public",
            "public_statistics",
            "public_raster_zonal",
            "raster_zonal",
            "cell_statistics",
            "cdse_public_statistics",
            "industrial",
            "batch_statistical",
            "industrial_batch_statistical",
            "sentinel_hub_batch_statistical",
        }
        if normalized not in aliases:
            raise ValueError("Unsupported Stage 4 provider adapter")
        return normalized

    @field_validator("stage4_public_source_adapter")
    @classmethod
    def validate_stage4_public_source_adapter(cls, value: str) -> str:
        from app.services.stage4_public_sources import normalize_public_source_adapter

        return normalize_public_source_adapter(value)

    @field_validator(
        "stage4_public_proof_max_cells",
        "stage4_public_statistics_max_cells_per_batch",
        "stage4_public_statistics_max_cells_per_run",
        "stage4_public_statistics_hard_ceiling_cells",
        "stage4_public_raster_zonal_max_cells_per_batch",
        "stage4_public_raster_zonal_source_tile_size_m",
        "stage4_public_raster_zonal_max_pixels",
        "stage4_public_stac_scene_limit",
    )
    @classmethod
    def validate_stage4_public_cell_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Stage 4 public proof cell limits must be greater than zero")
        return value

    @field_validator(
        "stage4_source_unavailable_circuit_cooldown_minutes",
        "stage4_source_unavailable_retry_seconds",
        "stage4_provider_max_concurrency",
    )
    @classmethod
    def validate_stage4_runtime_positive_ints(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Stage 4 runtime limits must be greater than zero")
        return value

    @field_validator("stage4_public_raster_zonal_resolution_m", "stage4_public_raster_zonal_min_free_gb")
    @classmethod
    def validate_stage4_public_raster_zonal_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Stage 4 public raster-zonal limits must be greater than zero")
        return value

    @field_validator("stage4_batch_statistical_max_features")
    @classmethod
    def validate_stage4_batch_statistical_max_features(cls, value: int) -> int:
        if value <= 0 or value > 700_000:
            raise ValueError("Stage 4 Batch Statistical jobs support 1 to 700000 features")
        return value

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def app_database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_app_user,
            password=self.postgres_app_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def supported_locales(self) -> list[str]:
        from app.services.localization import parse_supported_locales

        return list(parse_supported_locales(self.app_supported_locales))

    @property
    def default_locale(self) -> str:
        from app.services.localization import resolve_locale

        return resolve_locale(self.app_default_locale, self.supported_locales)

    @property
    def layer_a_source_layers(self) -> list[str]:
        return [
            source_layer.strip()
            for source_layer in self.stage1_layer_a_source_layers.split(",")
            if source_layer.strip()
        ]

    @property
    def verification_source_layers(self) -> list[str]:
        return [
            source_layer.strip()
            for source_layer in self.stage1_verification_source_layers.split(",")
            if source_layer.strip()
        ]

    @property
    def forest_mask_source_layers(self) -> list[str]:
        return [
            source_layer.strip()
            for source_layer in self.stage3_forest_mask_source_layers.split(",")
            if source_layer.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
