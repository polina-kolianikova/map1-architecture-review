import unittest
from unittest.mock import patch

from app.core.config import Settings


class SettingsTest(unittest.TestCase):
    def test_string_env_values_are_trimmed_and_unquoted(self):
        settings = Settings(
            cdse_password='"sample-value"',
            ogm_proxy_url="'socks5h://127.0.0.1:25344'",
        )

        self.assertEqual(settings.cdse_password, "sample-value")
        self.assertEqual(settings.ogm_proxy_url, "socks5h://127.0.0.1:25344")

    def test_stage3_v10_partitioned_reads_default_off(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings()

        self.assertFalse(settings.stage3_v10_partitioned_grid_reads)

    def test_stage3_partitioned_evidence_reads_default_off(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings()

        self.assertFalse(settings.stage3_partitioned_evidence_reads)

    def test_stage4_scheduler_default_off(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings()

        self.assertFalse(settings.stage4_scheduler_enabled)

    def test_stage4_defaults_target_accepted_v10_scope(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings()

        self.assertEqual(settings.stage4_region_code, "TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10")
        self.assertEqual(settings.stage4_grid_version, "stage3-public-ogm-osm-union-clean-v10")

    def test_stage4_defaults_to_public_stack_mode(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings()

        self.assertEqual(settings.stage4_processing_path, "public")
        self.assertEqual(settings.stage4_provider_adapter, "public_raster_zonal")
        self.assertEqual(settings.stage4_ndvi_processing_mode, "public_raster_zonal")
        self.assertEqual(settings.stage4_public_proof_max_cells, 10)
        self.assertEqual(settings.stage4_public_statistics_max_cells_per_batch, 10)
        self.assertEqual(settings.stage4_public_statistics_max_cells_per_run, 50)
        self.assertFalse(settings.stage4_public_statistics_allow_full_scope)
        self.assertEqual(settings.stage4_public_source_adapter, "public_stac_cog")
        self.assertTrue(settings.stage4_public_raster_cache_enabled)
        self.assertEqual(settings.stage4_public_raster_cache_dir, "/app/artifacts/stage4/public-raster-cache")
        self.assertEqual(settings.stage4_public_stac_api_url, "https://earth-search.aws.element84.com/v1/search")
        self.assertEqual(settings.stage4_public_stac_collection, "sentinel-2-l2a")
        self.assertEqual(settings.stage4_public_stac_scene_limit, 24)
        self.assertEqual(settings.stage4_batch_statistical_max_features, 700_000)
        self.assertEqual(settings.stage4_batch_statistical_s3_iam_role_arn, "")
        self.assertEqual(settings.stage4_batch_statistical_s3_access_key, "")
        self.assertEqual(settings.stage4_batch_statistical_s3_region, "WAW3-1")
        self.assertEqual(settings.stage4_batch_statistical_s3_endpoint_url, "")
        self.assertEqual(settings.stage4_work_dir, "/app/artifacts/stage4")
        self.assertEqual(settings.stage4_source_unavailable_circuit_cooldown_minutes, 15)
        self.assertEqual(settings.stage4_source_unavailable_retry_seconds, 900)
        self.assertEqual(settings.stage4_provider_max_concurrency, 1)

    def test_stage4_contract_cadence_accepts_five_to_ten_days(self):
        self.assertEqual(Settings(stage4_update_cadence_days=5).stage4_update_cadence_days, 5)
        self.assertEqual(Settings(stage4_update_cadence_days=10).stage4_update_cadence_days, 10)

    def test_stage4_contract_cadence_rejects_out_of_scope_values(self):
        with self.assertRaises(ValueError):
            Settings(stage4_update_cadence_days=4)
        with self.assertRaises(ValueError):
            Settings(stage4_update_cadence_days=11)

    def test_stage4_rejects_invalid_ndvi_processing_mode(self):
        with self.assertRaises(ValueError):
            Settings(stage4_ndvi_processing_mode="single_http_run")

    def test_stage4_rejects_invalid_processing_path(self):
        with self.assertRaises(ValueError):
            Settings(stage4_processing_path="commercial")

    def test_stage4_rejects_invalid_public_source_adapter(self):
        with self.assertRaises(ValueError):
            Settings(stage4_public_source_adapter="sentinel_hub_batch_api")


if __name__ == "__main__":
    unittest.main()
