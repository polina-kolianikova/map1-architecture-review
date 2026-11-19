import inspect
import unittest
from pathlib import Path

from app.api import stage2, stage4
from app.commands import fetch_cdse_ndvi, prepare_stage4_batch_statistical_inputs
from app.services import stage4_automation


class Stage4SchemaTest(unittest.TestCase):
    def test_stage4_migration_adds_required_tables_and_area_coefficient(self):
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260817_0031_stage4_layer_b_automation.py"
        )
        source = migration_path.read_text()

        self.assertIn("area_coefficient NUMERIC(14, 6)", source)
        self.assertIn("stage4_layer_b_runs", source)
        self.assertIn("stage4_layer_b_batches", source)
        self.assertIn("stage4_layer_b_events", source)
        self.assertIn("stage4_layer_b_metrics", source)
        self.assertIn("stage4_satellite_artifacts", source)
        self.assertIn("stage4_alerts", source)
        self.assertIn("sentinel_observations_partitioned_stage4_scope_grid_cell_date_idx", source)
        self.assertIn("GRANT SELECT, INSERT ON TABLE sentinel_observations_partitioned", source)
        self.assertIn("GRANT UPDATE (is_current) ON TABLE sentinel_observations_partitioned", source)

    def test_runtime_role_script_preserves_stage4_grants(self):
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "configure-db-runtime-role.sh"
        if not script_path.exists():
            self.skipTest("repository scripts are not mounted in this test image")
        source = script_path.read_text()

        self.assertIn("stage4_layer_b_runs", source)
        self.assertIn("stage4_layer_b_metrics", source)
        self.assertIn("sentinel_observations_partitioned", source)
        self.assertIn("UPDATE (area_coefficient)", source)

    def test_stage4_run_lookup_migration_indexes_proof_reads(self):
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260822_0032_stage4_run_lookup_indexes.py"
        )
        source = migration_path.read_text()

        self.assertIn("stage4_layer_b_metrics_run_status_idx", source)
        self.assertIn("(run_id, metric_name, source_status, grid_cell_id)", source)
        self.assertIn("stage4_satellite_artifacts_run_provider_type_idx", source)
        self.assertIn("stage4_alerts_run_status_idx", source)

    def test_public_proof_script_is_separate_from_full_scope_readiness(self):
        root = Path(__file__).resolve().parents[2]
        proof_script = root / "scripts" / "check-stage4-public-proof.sh"
        readiness_script = root / "scripts" / "check-stage4-automation.sh"
        if not proof_script.exists() or not readiness_script.exists():
            self.skipTest("repository scripts are not mounted in this test image")

        proof_source = proof_script.read_text()
        readiness_source = readiness_script.read_text()

        self.assertIn("run_stage4_public_proof", proof_source)
        self.assertIn("STAGE4_PUBLIC_PROOF_APPLY", proof_source)
        self.assertIn("check_stage4_automation", readiness_source)
        self.assertIn("--fail-on-blockers", readiness_source)

    def test_batch_statistical_input_export_uses_geopackage_identifier(self):
        source = inspect.getsource(prepare_stage4_batch_statistical_inputs)

        self.assertIn("ogr2ogr", source)
        self.assertIn("GPKG", source)
        self.assertIn("cell.id::text AS identifier", source)
        self.assertIn("area_coefficient", source)

    def test_compose_passes_stage4_s3_transfer_settings(self):
        root = Path(__file__).resolve().parents[2]

        for compose_file in (
            root / "infra" / "docker-compose.yml",
            root / "infra" / "docker-compose.prod.yml",
        ):
            if not compose_file.exists():
                self.skipTest("repository compose files are not mounted in this test image")
            source = compose_file.read_text()
            self.assertGreaterEqual(source.count("STAGE4_BATCH_STATISTICAL_S3_ACCESS_KEY"), 4)
            self.assertGreaterEqual(source.count("STAGE4_BATCH_STATISTICAL_S3_SECRET_ACCESS_KEY"), 4)
            self.assertGreaterEqual(source.count("STAGE4_BATCH_STATISTICAL_S3_REGION"), 4)
            self.assertGreaterEqual(source.count("STAGE4_BATCH_STATISTICAL_S3_ENDPOINT_URL"), 4)
            self.assertGreaterEqual(source.count("STAGE4_WORK_DIR"), 4)
            self.assertGreaterEqual(source.count("STAGE4_PUBLIC_SOURCE_ADAPTER"), 4)
            self.assertGreaterEqual(source.count("STAGE4_PUBLIC_RASTER_CACHE_ENABLED"), 4)
            self.assertGreaterEqual(source.count("STAGE4_PUBLIC_RASTER_CACHE_DIR"), 4)
            self.assertGreaterEqual(source.count("STAGE4_PUBLIC_STAC_API_URL"), 4)
            self.assertGreaterEqual(source.count("STAGE4_PUBLIC_STAC_COLLECTION"), 4)
            self.assertGreaterEqual(source.count("STAGE4_PUBLIC_STAC_SCENE_LIMIT"), 4)
            self.assertIn("STAGE4_SOURCE_UNAVAILABLE_RETRY_SECONDS", source)
            self.assertIn("STAGE4_PROVIDER_MAX_CONCURRENCY", source)

    def test_stage4_observations_use_separate_pipeline_name(self):
        self.assertEqual(stage4_automation.STAGE4_NDVI_PIPELINE_NAME, "stage4_layer_b_ndvi")

        source = inspect.getsource(fetch_cdse_ndvi.store_observations)

        self.assertIn("pipeline_name", source)
        self.assertIn('"pipeline": pipeline_name', source)

    def test_cdse_client_has_batch_statistical_path(self):
        from app.services.cdse import CdseClient

        source = inspect.getsource(CdseClient)

        self.assertIn("create_ndvi_batch_statistics_request", source)
        self.assertIn("sentinel_hub_batch_statistics_url", source)
        self.assertIn("input_features_uri", source)
        self.assertIn("output_uri", source)

    def test_stage2_asset_payload_exposes_area_coefficient(self):
        self.assertIn("area_coefficient", stage2.ASSET_PUBLIC_COLUMNS)
        self.assertIn(
            "COALESCE(cell.area_coefficient, ROUND(cell.area_ha::numeric, 6)) AS area_coefficient",
            inspect.getsource(stage2._asset_scope_sql),
        )

    def test_stage4_api_exposes_monitoring_surfaces(self):
        source = inspect.getsource(stage4)

        self.assertIn('@router.get("/status")', source)
        self.assertIn('@router.get("/runs")', source)
        self.assertIn('@router.get("/alerts")', source)
        self.assertIn('@router.get("/assets/{cell_uuid}/metrics")', source)
        self.assertIn("metric_coverage", source)
        self.assertIn("heartbeats", source)
        self.assertIn("recent_events", source)

    def test_stage4_api_alerts_are_scoped_to_active_grid(self):
        status_source = inspect.getsource(stage4.stage4_status)
        alert_count_source = inspect.getsource(stage4._open_alert_count)
        alerts_source = inspect.getsource(stage4.list_stage4_alerts)

        self.assertIn("_open_alert_count(db, params)", status_source)
        self.assertIn("run.region_code = :region_code", alert_count_source)
        self.assertIn("run.grid_version = :grid_version", alert_count_source)
        self.assertIn("run.region_code = :region_code", alerts_source)
        self.assertIn("run.grid_version = :grid_version", alerts_source)

    def test_stage4_status_uses_batch_rollup_for_metric_coverage(self):
        source = inspect.getsource(stage4._metric_coverage)

        self.assertIn("stage4_layer_b_batches", source)
        self.assertIn("'batch_rollup'", source)
        self.assertIn("exact_counts", source)
        self.assertNotIn("FROM stage4_layer_b_metrics", source)

    def test_stage4_public_api_hides_internal_run_fields(self):
        run_source = inspect.getsource(stage4.get_stage4_run)
        metric_source = inspect.getsource(stage4.get_stage4_asset_metrics)
        payload_source = inspect.getsource(stage4._public_run_payload)
        heartbeat_source = inspect.getsource(stage4._heartbeats)

        self.assertNotIn("celery_task_id", run_source)
        self.assertNotIn("source_product_id", metric_source)
        self.assertNotIn("run_id", heartbeat_source)
        self.assertNotIn("batch_id", heartbeat_source)
        self.assertIn("AS has_metadata", heartbeat_source)
        self.assertNotIn("SELECT\n                metadata", heartbeat_source)
        self.assertIn('payload.pop("metadata", None)', payload_source)
        self.assertIn('payload.pop("latest_error", None)', payload_source)

    def test_stage4_status_reports_physical_area_coefficient_backfill(self):
        source = inspect.getsource(stage4._area_coefficient_status)

        self.assertIn("WHERE cell.area_coefficient IS NOT NULL", source)
        self.assertIn("WHERE cell.area_coefficient IS NULL", source)
        self.assertNotIn("COALESCE(cell.area_coefficient", source)
        self.assertIn("stage4_run_preflight", source)


if __name__ == "__main__":
    unittest.main()
