import inspect
import json
import re
import sqlite3
import unittest
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.commands import (
    check_stage4_automation,
    check_stage4_public_scaling,
    prepare_stage4_public_raster_zonal_inputs,
    resume_stage4_run,
    run_stage4_automation_plan,
    run_stage4_public_proof,
)
from app.core.config import Settings
from app.services import stage4_automation, stage4_public_cog
from app.services.stage4_automation import (
    Stage4Scope,
    TASK_BIOMASS_UPDATE,
    TASK_NDVI_UPDATE,
    area_coefficient_from_area_ha,
    batch_ranges,
    biomass_from_ndvi,
    enabled_task_names,
    validate_task_names,
)
from app.services.stage4_public_sources import (
    public_raster_cache_identity,
    public_raster_cache_key,
    public_source_plan,
)


class Stage4AutomationTest(unittest.TestCase):
    def test_area_coefficient_uses_actual_cell_area(self):
        self.assertEqual(area_coefficient_from_area_ha("0.1234564"), Decimal("0.123456"))
        self.assertEqual(area_coefficient_from_area_ha("0.1234565"), Decimal("0.123457"))

    def test_area_coefficient_rejects_zero_area(self):
        with self.assertRaises(ValueError):
            area_coefficient_from_area_ha("0")

    def test_batch_ranges_cover_cell_id_span(self):
        self.assertEqual(batch_ranges(10, 25, 7), [(10, 16), (17, 23), (24, 25)])

    def test_effective_batch_size_respects_provider_limit(self):
        settings = Settings(
            stage4_processing_path="industrial",
            stage4_provider_adapter="industrial",
            stage4_ndvi_processing_mode="batch_statistical",
            stage4_batch_statistical_max_features=700_000,
        )

        self.assertEqual(stage4_automation.stage4_effective_batch_size(800_000, settings), 700_000)
        self.assertEqual(stage4_automation.stage4_effective_batch_size(500_000, settings), 500_000)

    def test_provider_permission_report_blocks_unauthorized_batch_statistical_account(self):
        fake_client = MagicMock()
        fake_client.get_access_token.return_value = "token"
        fake_public_response = MagicMock(status_code=200, headers={"content-type": "application/json"})
        fake_public_response.json.return_value = {"value": []}
        fake_client.session.get.return_value = fake_public_response
        fake_statistics_response = MagicMock(status_code=200, headers={"content-type": "application/json"})
        fake_client.check_ndvi_statistics_endpoint.return_value = fake_statistics_response

        with patch("app.services.stage4_providers.CdseClient", return_value=fake_client):
            report = check_stage4_automation._provider_permission_report(
                Settings(
                    stage4_provider_adapter="public_statistics",
                    stage4_ndvi_processing_mode="public_statistics",
                )
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["provider_adapter"], "cdse_public_statistics")
        self.assertEqual(report["catalogue_status_code"], 200)
        self.assertEqual(report["statistics_status_code"], 200)
        fake_client.check_ndvi_statistics_endpoint.assert_called_once()

        fake_client = MagicMock()
        fake_client.get_access_token.return_value = "token"
        fake_response = MagicMock(status_code=403, headers={"content-type": "application/json"})
        fake_response.json.return_value = {
            "error": {"code": "COMMON_INSUFFICIENT_PERMISSIONS"},
        }
        fake_client.session.get.return_value = fake_response

        with patch("app.services.stage4_providers.CdseClient", return_value=fake_client):
            report = check_stage4_automation._provider_permission_report(
                Settings(
                    stage4_processing_path="industrial",
                    stage4_provider_adapter="industrial",
                    stage4_ndvi_processing_mode="batch_statistical",
                )
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status_code"], 403)
        self.assertIn("not authorized", report["blocker"])

    def test_enabled_tasks_follow_settings(self):
        settings = Settings(stage4_ndvi_update_enabled=True, stage4_biomass_update_enabled=False)

        self.assertEqual(enabled_task_names(settings), [TASK_NDVI_UPDATE])

    def test_task_names_are_validated_and_deduplicated(self):
        self.assertEqual(
            validate_task_names([TASK_NDVI_UPDATE, TASK_NDVI_UPDATE, TASK_BIOMASS_UPDATE]),
            [TASK_NDVI_UPDATE, TASK_BIOMASS_UPDATE],
        )
        with self.assertRaises(ValueError):
            validate_task_names(["unsupported"])

    def test_private_biomass_calculation_is_unavailable(self):
        with self.assertRaisesRegex(RuntimeError, "not distributed"):
            biomass_from_ndvi(None, settings=Settings(stage4_biomass_formula_enabled=False))

    def test_biomass_configuration_does_not_enable_private_calculation(self):
        with self.assertRaisesRegex(RuntimeError, "not distributed"):
            biomass_from_ndvi(None, settings=Settings(stage4_biomass_formula_enabled=True))

    def test_metrics_store_area_weighted_value_for_stage5(self):
        import unittest
        raise unittest.SkipTest("Private calculation inputs or expected outputs are not distributed.")

    def test_stage4_ndvi_mirrors_into_partitioned_evidence_when_available(self):
        source = inspect.getsource(stage4_automation.mirror_stage4_observations_to_partitioned)

        self.assertIn("sentinel_observations_partitioned", source)
        self.assertIn("STAGE4_NDVI_PIPELINE_NAME", source)
        self.assertIn("SET is_current = source_rows.is_current", source)
        self.assertIn("partitioned.region_id = source_rows.region_id", source)
        self.assertIn("INSERT INTO sentinel_observations_partitioned (", source)
        self.assertIn("NOT EXISTS", source)

    def test_stage4_queues_ndvi_before_biomass(self):
        source = inspect.getsource(stage4_automation.load_queued_batch_ids)

        self.assertIn("WHEN 'ndvi_update' THEN 1", source)
        self.assertIn("WHEN 'biomass_update' THEN 2", source)

    def test_stage4_worker_waits_for_enqueue_transaction_lock(self):
        source = inspect.getsource(stage4_automation._claim_batch)

        self.assertIn("FOR UPDATE", source)
        self.assertNotIn("SKIP LOCKED", source)

    def test_stage4_source_unavailable_retries_without_failing_batch(self):
        source = inspect.getsource(stage4_automation.process_batch)
        task_source = (Path(__file__).resolve().parents[1] / "app/tasks/stage4.py").read_text()
        exc = RuntimeError("CDSE auth failed")
        exc.response = SimpleNamespace(
            status_code=403,
            url="https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        )

        self.assertTrue(stage4_automation._is_stage4_source_unavailable(exc))
        self.assertIn("_source_unavailable_circuit_open", source)
        self.assertIn("_defer_batch_for_source_unavailable", source)
        self.assertIn("source_unavailable", task_source)
        self.assertIn("stage4_source_unavailable_retry_seconds", task_source)

    def test_public_ndvi_catalogue_search_uses_batch_extent(self):
        source = inspect.getsource(stage4_automation.process_public_statistics_ndvi_batch)

        self.assertIn("_load_batch_area_wkt", source)
        self.assertIn("area_wkt=batch_wkt", source)
        self.assertNotIn("load_region_wkt", source)

    def test_public_proof_command_has_strict_acceptance_gate(self):
        source = inspect.getsource(run_stage4_public_proof)

        self.assertIn('capabilities.adapter == "public_raster_zonal"', source)
        self.assertIn("stage4_public_statistics_max_cells_per_run", source)
        self.assertIn("Stage 4 public proof is capped", source)
        self.assertIn("_proof_acceptance_report", source)
        self.assertIn("min_ndvi_ok_ratio", source)
        self.assertIn("NDVI proof did not meet the required public-data coverage ratio", source)
        self.assertIn("NDVI proof did not succeed for every expected cell", source)
        self.assertIn("Biomass proof is not explicitly pending_formula", source)
        self.assertIn("Stage 4 public proof failed acceptance gate", source)

    def test_stage4_orchestration_avoids_top_level_provider_imports(self):
        source = inspect.getsource(stage4_automation).split("STAGE4_NDVI_PIPELINE_NAME", maxsplit=1)[0]

        self.assertNotIn("from app.commands import fetch_cdse_ndvi", source)
        self.assertNotIn("from app.services.cdse import CdseClient", source)
        self.assertNotIn("from app.services.stage4_storage", source)

    def test_stage4_biomass_waits_for_ndvi_batch(self):
        source = inspect.getsource(stage4_automation.process_batch)

        self.assertIn("_batch_waits_for_ndvi", source)
        self.assertIn("waiting_for_ndvi", source)

    def test_full_scope_ndvi_uses_provider_adapter(self):
        source = inspect.getsource(stage4_automation.process_batch)

        self.assertIn("get_stage4_ndvi_provider", source)
        self.assertIn("provider.capabilities.uses_external_jobs", source)

    def test_public_statistics_worker_enforces_scope_limits(self):
        source = inspect.getsource(stage4_automation.process_batch)
        guard = inspect.getsource(stage4_automation.validate_public_statistics_batch_limits)

        self.assertIn("validate_public_statistics_batch_limits", source)
        self.assertIn("_count_batch_cells", guard)
        self.assertIn("stage4_public_statistics_max_cells_per_batch", guard)
        self.assertIn("stage4_public_statistics_max_cells_per_run", guard)
        self.assertIn("stage4_public_statistics_hard_ceiling_cells", guard)
        self.assertIn("stage4_public_statistics_allow_full_scope", guard)
        self.assertIn("PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER", guard)
        self.assertIn("validate_public_raster_zonal_batch_limits", source)

    def test_provider_capabilities_mark_public_statistics_as_diagnostic_only(self):
        public_statistics = stage4_automation.stage4_provider_capabilities(
            Settings(
                stage4_provider_adapter="public_statistics",
                stage4_ndvi_processing_mode="public_statistics",
            )
        )
        public_raster = stage4_automation.stage4_provider_capabilities(
            Settings(stage4_provider_adapter="public_raster_zonal", stage4_ndvi_processing_mode="public_raster_zonal")
        )

        self.assertTrue(public_statistics.diagnostic_only)
        self.assertFalse(public_statistics.supports_full_scope)
        self.assertEqual(public_statistics.batching_strategy, "cell_range")
        self.assertFalse(public_raster.diagnostic_only)
        self.assertTrue(public_raster.supports_full_scope)
        self.assertEqual(public_raster.batching_strategy, "stage3_spatial_tiles")
        self.assertEqual(public_raster.public_source_adapter, "public_stac_cog")
        self.assertFalse(public_raster.uses_batch_api)
        self.assertFalse(public_raster.uses_commercial_storage)
        self.assertTrue(public_raster.shared_raster_cache_supported)

    def test_public_statistics_full_scope_hard_block_survives_limit_overrides(self):
        scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=20_953_582,
            min_cell_id=1,
            max_cell_id=20_953_582,
            missing_area_coefficient_cells=0,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_provider_adapter="public_statistics",
                stage4_ndvi_processing_mode="public_statistics",
                stage4_public_statistics_allow_full_scope=True,
                stage4_cell_statistics_max_cells=99_999_999,
                stage4_public_statistics_max_cells_per_run=99_999_999,
            ),
        )

        self.assertIn(stage4_automation.PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER, blockers)

    def test_public_raster_zonal_uses_stage3_spatial_tile_batches(self):
        source = inspect.getsource(stage4_automation)
        table_guard = inspect.getsource(stage4_automation._table_exists)

        self.assertIn("stage3_spatial_tiles", source)
        self.assertIn("_create_stage4_spatial_tile_batches", source)
        self.assertIn("ST_PointOnSurface(ST_Transform(cell.geom", source)
        self.assertIn("bbox_projected", source)
        self.assertIn("stage3_grid_generation_runs", table_guard)
        self.assertIn("stage3_grid_generation_tiles", table_guard)

    def test_public_raster_zonal_import_writes_observations_and_metrics(self):
        source = inspect.getsource(stage4_automation.process_public_raster_zonal_ndvi_batch)
        bulk = inspect.getsource(stage4_automation._bulk_import_public_raster_zonal_rows)
        mirror = inspect.getsource(stage4_automation._mirror_staged_public_raster_zonal_observations_to_partitioned)

        self.assertIn("ensure_public_raster_zonal_results", source)
        self.assertIn("_stage_public_raster_zonal_rows", source)
        self.assertIn("_bulk_import_public_raster_zonal_rows", source)
        self.assertIn("tmp_stage4_public_raster_zonal_rows", bulk)
        self.assertIn("tmp_stage4_public_raster_zonal_observations", bulk)
        self.assertIn("INSERT INTO sentinel_observations", bulk)
        self.assertIn("INSERT INTO stage4_layer_b_metrics", bulk)
        self.assertIn("'public_raster_zonal'", bulk)
        self.assertIn("source_window_start", bulk)
        self.assertIn("source_window_end", bulk)
        self.assertIn("source_provenance", bulk)
        self.assertIn("stage4_public_raster_zonal_partitioned_mirror_enabled", bulk)
        self.assertIn("FROM tmp_stage4_public_raster_zonal_observations staged", mirror)
        self.assertIn("partitioned.region_id = :region_id", mirror)
        self.assertNotIn("FROM sentinel_observations observation", mirror)
        self.assertIn("_record_satellite_artifact", source)
        self.assertIn('"artifact_state": "validated"', source)
        self.assertIn('"import_state": "import_done"', source)

    def test_public_raster_zonal_has_local_processor_before_import(self):
        source = inspect.getsource(stage4_automation.ensure_public_raster_zonal_results)
        generator = inspect.getsource(stage4_automation._generate_public_raster_zonal_results)
        source_raster = inspect.getsource(stage4_automation._ensure_public_raster_source_raster)
        manifest = inspect.getsource(stage4_automation._public_raster_zonal_manifest_identity)
        zonal = inspect.getsource(stage4_automation._calculate_public_raster_zonal_rows)
        queue_loader = inspect.getsource(stage4_automation.load_queued_batch_ids)

        self.assertIn("_public_raster_zonal_result_output_path", source)
        self.assertIn("_validate_public_raster_zonal_manifest", source)
        self.assertIn("_public_raster_zonal_results_path", source)
        self.assertIn("stage4_public_raster_zonal_reuse_existing_csv", source)
        self.assertIn("_ensure_public_raster_source_raster", generator)
        self.assertIn("build_public_stac_ndvi_raster", source_raster)
        self.assertIn("request_ndvi_raster", source_raster)
        self.assertIn("window=window", zonal)
        self.assertIn("window_transform", zonal)
        self.assertIn("grid_size_m", zonal)
        self.assertIn("load_valid_public_raster_cache", source_raster)
        self.assertIn("write_public_raster_cache", source_raster)
        self.assertIn("write_public_raster_cache_file", source_raster)
        self.assertIn("source_cache_ready", queue_loader)
        self.assertIn("source_tile_batch_rank", queue_loader)
        self.assertIn("_public_raster_zonal_cache_hit_result", source_raster)
        self.assertLess(
            source_raster.index("load_valid_public_raster_cache(settings, identity)"),
            source_raster.index("with _public_raster_cache_generation_lock"),
        )
        self.assertIn("_calculate_public_raster_zonal_rows", generator)
        self.assertIn("_write_public_raster_zonal_manifest", generator)
        self.assertIn("run_uuid", manifest)
        self.assertIn("source_adapter", manifest)
        self.assertIn("bbox", manifest)
        self.assertIn("raster_path.unlink", generator)
        self.assertIn("_public_raster_zonal_source_bbox", generator)
        self.assertIn("_public_raster_cache_generation_lock", source_raster)
        self.assertIn("_public_raster_zonal_target_mask", source_raster)
        self.assertIn("target_mask", source_raster)
        self.assertIn("target_mask_sha256=target_mask_sha256", source_raster)
        self.assertLess(
            source_raster.index("load_valid_public_raster_cache"),
            source_raster.index("_public_raster_zonal_target_mask"),
        )
        self.assertIn("features.rasterize", zonal)
        self.assertIn("np.bincount", zonal)
        self.assertIn("all_touched=False", zonal)

    def test_public_stac_cog_mosaic_stops_after_target_mask_coverage(self):
        try:
            import numpy as np
            import rasterio
        except ImportError:
            self.skipTest("rasterio/numpy runtime dependencies are not installed")

        scene = {
            "id": "scene-1",
            "assets": {
                "red": {"href": "red.tif", "raster:bands": [{"scale": 1, "offset": 0}]},
                "nir": {"href": "nir.tif", "raster:bands": [{"scale": 1, "offset": 0}]},
                "scl": {"href": "scl.tif", "raster:bands": [{"scale": 1, "offset": 0}]},
            },
        }
        scenes = [scene, {**scene, "id": "scene-2"}]
        target_mask = np.zeros((256, 256), dtype=bool)
        target_mask[128, 128] = True
        calls = []

        def fake_read_asset(scene, asset_key, *_args):
            calls.append((scene["id"], asset_key))
            if asset_key == "scl":
                array = np.full((256, 256), 4, dtype="uint8")
            elif asset_key == "red":
                array = np.ones((256, 256), dtype="float32")
            else:
                array = np.full((256, 256), 2, dtype="float32")
            return {"array": array, "valid": np.ones((256, 256), dtype=bool)}

        with TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "ndvi.tif"
            with patch("app.services.stage4_public_cog._read_asset", side_effect=fake_read_asset):
                stats = stage4_public_cog._write_ndvi_mosaic(
                    output_path,
                    scenes=scenes,
                    bbox=[0, 0, 256, 256],
                    crs_epsg=3857,
                    width=256,
                    height=256,
                    http_timeout_seconds=1,
                    target_mask=target_mask,
                )

            with rasterio.open(output_path) as dataset:
                valid = dataset.read(2, masked=False)

        self.assertEqual(stats["coverage_strategy"], "target_mask")
        self.assertEqual(stats["scenes_attempted"], 1)
        self.assertEqual(stats["target_valid_pixel_count"], 1)
        self.assertTrue(stats["target_coverage_complete"])
        self.assertEqual(int(valid.sum()), 1)
        self.assertEqual(calls, [("scene-1", "red"), ("scene-1", "nir"), ("scene-1", "scl")])

    def test_public_stac_cog_mosaic_waits_for_each_cell_zone(self):
        try:
            import numpy as np
            import rasterio
        except ImportError:
            self.skipTest("rasterio/numpy runtime dependencies are not installed")

        scene = {
            "id": "scene-1",
            "assets": {
                "red": {"href": "red.tif", "raster:bands": [{"scale": 1, "offset": 0}]},
                "nir": {"href": "nir.tif", "raster:bands": [{"scale": 1, "offset": 0}]},
                "scl": {"href": "scl.tif", "raster:bands": [{"scale": 1, "offset": 0}]},
            },
        }
        scenes = [scene, {**scene, "id": "scene-2"}]
        target_mask = {
            "zones": np.array([[1, 2]], dtype="int32"),
            "sample_counts": [0, 1, 1],
            "required_counts": [0, 1, 1],
            "min_valid_pixel_ratio": 1.0,
        }
        calls = []

        def fake_read_asset(scene, asset_key, *_args):
            calls.append((scene["id"], asset_key))
            if asset_key == "scl":
                array = (
                    np.array([[4, 0]], dtype="uint8")
                    if scene["id"] == "scene-1"
                    else np.array([[0, 4]], dtype="uint8")
                )
            elif asset_key == "red":
                array = np.ones((1, 2), dtype="float32")
            else:
                array = np.full((1, 2), 2, dtype="float32")
            return {"array": array, "valid": np.ones((1, 2), dtype=bool)}

        with TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "ndvi.tif"
            with patch("app.services.stage4_public_cog._read_asset", side_effect=fake_read_asset):
                stats = stage4_public_cog._write_ndvi_mosaic(
                    output_path,
                    scenes=scenes,
                    bbox=[0, 0, 2, 1],
                    crs_epsg=3857,
                    width=2,
                    height=1,
                    http_timeout_seconds=1,
                    target_mask=target_mask,
                )

            with rasterio.open(output_path) as dataset:
                valid = dataset.read(2, masked=False)

        self.assertEqual(stats["coverage_strategy"], "cell_valid_ratio")
        self.assertEqual(stats["scenes_attempted"], 2)
        self.assertEqual(stats["target_cell_count"], 2)
        self.assertEqual(stats["target_satisfied_cell_count"], 2)
        self.assertTrue(stats["target_coverage_complete"])
        self.assertEqual(int(valid.sum()), 2)
        self.assertEqual(len(calls), 6)

    def test_public_stac_cog_mosaic_fails_when_all_scenes_error(self):
        try:
            import numpy as np
            import rasterio  # noqa: F401
        except ImportError:
            self.skipTest("rasterio/numpy runtime dependencies are not installed")

        scene = {
            "id": "bad-scene",
            "assets": {
                "red": {"href": "red.tif"},
                "nir": {"href": "nir.tif"},
                "scl": {"href": "scl.tif"},
            },
        }
        target_mask = np.ones((1, 1), dtype=bool)
        heartbeats = []

        with TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "ndvi.tif"
            with patch("app.services.stage4_public_cog._read_asset", side_effect=OSError("boom")), \
                 patch("app.services.stage4_public_cog.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "retry the batch"):
                    stage4_public_cog._write_ndvi_mosaic(
                        output_path,
                        scenes=[scene],
                        bbox=[0, 0, 1, 1],
                        crs_epsg=3857,
                        width=1,
                        height=1,
                        http_timeout_seconds=1,
                        target_mask=target_mask,
                        heartbeat=lambda: heartbeats.append("beat"),
                    )

        self.assertEqual(heartbeats, ["beat", "beat", "beat"])

    def test_public_raster_zonal_target_mask_preserves_cell_zones(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy runtime dependency is not installed")

        cells = [
            {
                "id": 1,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
                },
            },
            {
                "id": 2,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[(1, 0), (2, 0), (2, 1), (1, 1), (1, 0)]],
                },
            },
        ]

        target_mask = stage4_automation._public_raster_zonal_target_mask(
            cells=cells,
            bbox=[0, 0, 2, 1],
            crs_epsg=4326,
            width=2,
            height=1,
            min_valid_pixel_ratio=1.0,
        )

        zones = target_mask["mask"]["zones"]
        self.assertEqual(set(int(value) for value in np.unique(zones)), {1, 2})
        self.assertEqual(target_mask["mask"]["sample_counts"][1:3], [1, 1])
        self.assertEqual(target_mask["mask"]["required_counts"][1:3], [1, 1])

    def test_public_source_adapter_plan_keeps_client_public_stack_boundary(self):
        settings = Settings(
            stage4_provider_adapter="public_raster_zonal",
            stage4_ndvi_processing_mode="public_raster_zonal",
            stage4_public_raster_cache_enabled=True,
        )

        plan = public_source_plan(settings)

        self.assertEqual(plan["adapter"], "public_stac_cog")
        self.assertFalse(plan["uses_account_auth"])
        self.assertFalse(plan["uses_batch_api"])
        self.assertFalse(plan["uses_commercial_storage"])
        self.assertTrue(plan["supports_shared_raster_cache"])
        self.assertEqual(plan["artifact_states"], ["raster_cached", "zonal_done", "import_done", "validated"])
        self.assertEqual(plan["blockers"], [])

    def test_account_authenticated_public_source_is_not_client_approved(self):
        plan = public_source_plan(
            Settings(
                stage4_provider_adapter="public_raster_zonal",
                stage4_ndvi_processing_mode="public_raster_zonal",
                stage4_public_source_adapter="cdse_process",
                stage4_public_raster_cache_enabled=True,
            )
        )

        self.assertIn(
            "Client-approved public Stage 4 source adapter must not require account authentication",
            plan["blockers"],
        )

    def test_cdse_process_source_is_blocked_for_client_public_stack(self):
        scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=20_953_582,
            min_cell_id=1,
            max_cell_id=20_953_582,
            missing_area_coefficient_cells=0,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_provider_adapter="public_raster_zonal",
                stage4_ndvi_processing_mode="public_raster_zonal",
                stage4_public_source_adapter="cdse_process",
                stage4_public_raster_cache_enabled=True,
            ),
        )

        self.assertIn(
            "CDSE Process API source adapter is not allowed for client-approved public Stage 4; use the public STAC COG source adapter",
            blockers,
        )
        with self.assertRaisesRegex(RuntimeError, "client-approved public Stage 4"):
            stage4_automation.validate_public_raster_zonal_batch_limits(
                {"total_cells": 1},
                {"id": 1, "expected_cells": 1, "task_name": TASK_NDVI_UPDATE},
                Settings(
                    stage4_provider_adapter="public_raster_zonal",
                    stage4_ndvi_processing_mode="public_raster_zonal",
                    stage4_public_source_adapter="cdse_process",
                    stage4_public_raster_cache_enabled=True,
                ),
            )

    def test_public_raster_cache_key_is_source_based_not_run_based(self):
        settings = Settings(stage4_public_raster_cache_enabled=True)
        run_a = {
            "run_uuid": "run-a",
            "requested_start": "2026-05-01",
            "requested_end": "2026-08-01",
            "max_cloud_coverage": 40,
        }
        run_b = {**run_a, "run_uuid": "run-b"}
        batch_a = {"id": 1, "batch_index": 1}
        batch_b = {"id": 2, "batch_index": 99}

        identity_a = public_raster_cache_identity(
            run=run_a,
            batch=batch_a,
            settings=settings,
            bbox=[0, 0, 10, 10],
            crs_epsg=3857,
            width=100,
            height=100,
        )
        identity_b = public_raster_cache_identity(
            run=run_b,
            batch=batch_b,
            settings=settings,
            bbox=[0, 0, 10, 10],
            crs_epsg=3857,
            width=100,
            height=100,
        )

        self.assertNotIn("run_uuid", identity_a)
        self.assertNotIn("batch_id", identity_a)
        self.assertEqual(identity_a["stac_scene_limit"], settings.stage4_public_stac_scene_limit)
        self.assertEqual(identity_a["source_algorithm"], "public-stac-cog-shared-source-tile-mosaic-v11")
        self.assertEqual(identity_a["target_mask_sha256"], "full-raster")
        self.assertEqual(identity_a["target_mask_strategy"], "full_raster")
        self.assertEqual(public_raster_cache_key(identity_a), public_raster_cache_key(identity_b))
        masked_identity = public_raster_cache_identity(
            run=run_a,
            batch=batch_a,
            settings=settings,
            bbox=[0, 0, 10, 10],
            crs_epsg=3857,
            width=100,
            height=100,
            target_mask_strategy="grid_cell_ids_in_source_tile_v2",
            target_mask_scope={"region_code": "TR", "grid_version": "stage3"},
            target_mask_sha256="mask-sha",
            target_pixel_count=2500,
            target_min_valid_pixel_ratio=0.7,
        )
        self.assertEqual(masked_identity["target_mask_sha256"], "mask-sha")
        self.assertEqual(masked_identity["target_pixel_count"], 2500)
        self.assertNotEqual(public_raster_cache_key(identity_a), public_raster_cache_key(masked_identity))

        wider_scene_identity = public_raster_cache_identity(
            run=run_a,
            batch=batch_a,
            settings=Settings(stage4_public_raster_cache_enabled=True, stage4_public_stac_scene_limit=48),
            bbox=[0, 0, 10, 10],
            crs_epsg=3857,
            width=100,
            height=100,
        )
        self.assertNotEqual(public_raster_cache_key(identity_a), public_raster_cache_key(wider_scene_identity))

    def test_public_raster_zonal_retries_reuse_current_run_csv(self):
        with TemporaryDirectory() as tmp_dir:
            settings = Settings(stage4_public_raster_zonal_results_dir=tmp_dir)
            run = {"run_uuid": "stage4-run", "region_code":"TR-PROOF", "grid_version":"proof",
                   "requested_start":"2026-09-17", "requested_end":"2026-11-01",
                   "max_cloud_coverage":40, "min_valid_pixel_ratio":0.35}
            batch = {"id":7, "batch_index": 7, "task_name":"ndvi_update"}
            result_path = Path(tmp_dir) / "stage4-run" / "batch-00007.csv"
            result_path.parent.mkdir(parents=True)
            result_path.write_text("cell_id,source_status\n1,ok\n")
            stage4_automation._write_public_raster_zonal_manifest(result_path, run=run, batch=batch,
                settings=settings, bbox=[0,0,10,10], crs_epsg=6933, row_count=1)

            with patch("app.services.stage4_automation._public_raster_zonal_results_path") as legacy_path, \
                 patch("app.services.stage4_automation._public_raster_zonal_bbox", return_value=([0,0,10,10],6933)):
                with patch("app.services.stage4_automation._generate_public_raster_zonal_results") as generator:
                    self.assertEqual(
                        stage4_automation.ensure_public_raster_zonal_results(run, batch, settings),
                        result_path,
                    )

            legacy_path.assert_not_called()
            generator.assert_not_called()

            manifest_path = stage4_automation._public_raster_zonal_manifest_path(result_path)
            payload = json.loads(manifest_path.read_text())
            payload['source_algorithm'] = 'public-stac-cog-shared-source-tile-mosaic-v8'
            manifest_path.write_text(json.dumps(payload))
            with patch("app.services.stage4_automation._public_raster_zonal_bbox", return_value=([0,0,10,10],6933)):
                with self.assertRaisesRegex(RuntimeError, "does not match current run"):
                    stage4_automation.ensure_public_raster_zonal_results(run,batch,settings)
            manifest_path.unlink()
            with self.assertRaisesRegex(RuntimeError, "manifest is missing"):
                stage4_automation._validate_public_raster_zonal_manifest(
                    result_path, run=run, batch=batch, settings=settings,
                )

    def test_public_raster_zonal_ok_row_requires_ndvi(self):
        with self.assertRaises(ValueError):
            stage4_automation._public_raster_zonal_ndvi({"source_status": "ok", "ndvi": ""}, "ok")

    def test_public_raster_zonal_worker_enforces_batch_limits(self):
        source = inspect.getsource(stage4_automation.validate_public_raster_zonal_batch_limits)

        self.assertIn("_count_batch_cells", source)
        self.assertIn("stage4_public_raster_zonal_max_cells_per_batch", source)
        self.assertIn("stage4_public_raster_cache_enabled", source)
        self.assertIn("Shared public raster cache is required before full-scope public raster-zonal runs", source)
        self.assertIn("Public raster-zonal batch", source)

    def test_public_raster_zonal_plan_blocks_oversized_stage3_tiles(self):
        source = inspect.getsource(stage4_automation.stage4_batching_context)
        summary_source = inspect.getsource(stage4_automation._stage3_spatial_tile_summary)
        count_source = inspect.getsource(stage4_automation._stage3_spatial_tile_rows_from_counts)

        self.assertIn("max_tile_cells", source)
        self.assertIn("spatial tile exceeds configured cells-per-batch limit", source)
        self.assertIn("max(tile.inserted_cells)", summary_source)
        self.assertIn("CAST(:min_cell_id AS bigint)", count_source)
        self.assertIn("CAST(:max_cell_id AS bigint)", count_source)

    def test_public_raster_zonal_spatial_filter_prevents_boundary_duplicates(self):
        source = inspect.getsource(stage4_automation._spatial_tile_batch_sql)
        load_source = inspect.getsource(stage4_automation._load_batch_cells)
        count_source = inspect.getsource(stage4_automation._count_batch_cells)
        export_source = inspect.getsource(prepare_stage4_public_raster_zonal_inputs._export_batch_cells_gpkg)

        self.assertIn("point_projected", source)
        self.assertIn("ST_X(stage4_cell.point_projected) >= :tile_min_x", source)
        self.assertIn("ST_X(stage4_cell.point_projected) < :tile_max_x", source)
        self.assertIn("ST_Y(stage4_cell.point_projected) >= :tile_min_y", source)
        self.assertIn("ST_Y(stage4_cell.point_projected) < :tile_max_y", source)
        self.assertIn("_validate_spatial_tile_cell_count", load_source)
        self.assertIn("_validate_spatial_tile_cell_count", count_source)
        self.assertIn("_stage4_grid_cells_table", load_source)
        self.assertIn("earlier_tile.id <", export_source)

    def test_public_raster_zonal_prepare_exports_spatial_batch_inputs(self):
        source = inspect.getsource(prepare_stage4_public_raster_zonal_inputs)

        self.assertIn("ogr2ogr", source)
        self.assertIn("stage3_spatial_tiles", source)
        self.assertIn("cells.gpkg", source)
        self.assertIn("manifest.json", source)
        self.assertIn("result_csv", source)
        self.assertNotIn("batch_statistics", source)
        self.assertNotIn("upload_s3", source)

    def test_public_raster_zonal_readiness_requires_successful_proof(self):
        source = inspect.getsource(check_stage4_automation)
        proof_gate = inspect.getsource(check_stage4_automation._latest_public_raster_zonal_proof)
        proof_source = inspect.getsource(run_stage4_public_proof)

        self.assertIn("_latest_public_raster_zonal_proof", source)
        self.assertIn("successful small tile proof", source)
        self.assertIn("accepted", source)
        self.assertIn("ndvi_ok_ratio", source)
        self.assertIn(">= 0.9", source)
        self.assertIn("metadata->>'accepted'", proof_gate)
        self.assertNotIn('ndvi_metric_cells"] or 0) == int(payload["total_cells"', proof_gate)
        self.assertIn("_record_public_proof_acceptance", proof_source)

    def test_public_path_blocks_s3_history_storage(self):
        scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=10,
            min_cell_id=1,
            max_cell_id=10,
            missing_area_coefficient_cells=0,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_provider_adapter="public_statistics",
                stage4_ndvi_processing_mode="public_statistics",
                stage4_history_storage_uri="s3://map1-stage4/history",
            ),
        )

        self.assertIn("S3 history storage must stay empty while the public Stage 4 path is active", blockers)

    def test_full_scope_public_raster_zonal_requires_shared_cache(self):
        scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=20_953_582,
            min_cell_id=1,
            max_cell_id=20_953_582,
            missing_area_coefficient_cells=0,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_provider_adapter="public_raster_zonal",
                stage4_ndvi_processing_mode="public_raster_zonal",
                stage4_public_raster_cache_enabled=False,
            ),
        )

        self.assertIn("Shared public raster cache is required before full-scope public raster-zonal runs", blockers)

    def test_public_scaling_report_keeps_public_statistics_out_of_full_scope(self):
        report = check_stage4_public_scaling._scaling_report(
            scope_total_cells=20_953_582,
            cadence_days=10,
            hard_ceiling_cells=100_000,
            active_provider_adapter="cdse_public_statistics",
            latest_probe={
                "id": 7,
                "run_uuid": "probe",
                "status": "completed",
                "total_cells": 500,
                "succeeded_cells": 496,
                "skipped_cells": 504,
                "requested_start": "2026-05-01",
                "requested_end": "2026-08-01",
                "max_cloud_coverage": 80,
                "min_valid_pixel_ratio": 0.35,
                "started_at": "2026-08-19T11:04:45Z",
                "finished_at": "2026-08-19T11:29:51Z",
                "ndvi_duration_seconds": 1507,
            },
            metric_statuses=[
                {"metric_name": "ndvi", "source_status": "ok", "cells": 496, "rows": 496},
                {"metric_name": "ndvi", "source_status": "no_valid_pixels", "cells": 4, "rows": 4},
            ],
        )

        self.assertFalse(report["ready_for_full_scope"])
        self.assertEqual(report["recommended_provider_adapter"], "public_raster_zonal")
        self.assertIn(stage4_automation.PUBLIC_STATISTICS_FULL_SCOPE_BLOCKER, report["blockers"])

    def test_public_scaling_report_accepts_raster_zonal_proof_and_plan(self):
        report = check_stage4_public_scaling._scaling_report(
            scope_total_cells=20_953_582,
            cadence_days=10,
            hard_ceiling_cells=100_000,
            active_provider_adapter="public_raster_zonal",
            latest_probe=None,
            metric_statuses=[],
            raster_zonal_proof={"required": True, "ok": True, "latest": {"ndvi_ok_ratio": 0.94}},
            raster_zonal_plan={
                "provider_adapter": "public_raster_zonal",
                "supports_full_scope": True,
                "diagnostic_only": False,
                "batching_strategy": "stage3_spatial_tiles",
                "total_cells": 20_953_582,
                "total_batches": 11_798,
                "spatial_tile_count": 5_899,
                "spatial_tile_max_cells": 10_000,
                "blockers": [],
            },
            raster_zonal_timing={
                "completed_batches": 115,
                "failed_batches": 0,
                "processed_cells": 493_074,
                "ndvi_duration_seconds": 1800,
                "open_alerts": 0,
            },
            public_source_plan_info={"shared_raster_cache_enabled": True, "blockers": []},
        )

        self.assertTrue(report["ready_for_full_scope"])
        self.assertEqual(report["recommended_provider_adapter"], "public_raster_zonal")
        self.assertEqual(report["public_raster_zonal_plan"]["batching_strategy"], "stage3_spatial_tiles")
        self.assertGreater(report["public_raster_zonal_throughput"]["cells_per_hour"], 0)
        self.assertEqual(report["blockers"], [])

    def test_public_scaling_report_blocks_raster_zonal_without_timing(self):
        report = check_stage4_public_scaling._scaling_report(
            scope_total_cells=20_953_582,
            cadence_days=10,
            hard_ceiling_cells=100_000,
            active_provider_adapter="public_raster_zonal",
            latest_probe=None,
            metric_statuses=[],
            raster_zonal_proof={"required": True, "ok": True, "latest": {"ndvi_ok_ratio": 0.94}},
            raster_zonal_plan={
                "provider_adapter": "public_raster_zonal",
                "supports_full_scope": True,
                "diagnostic_only": False,
                "batching_strategy": "stage3_spatial_tiles",
                "total_cells": 20_953_582,
                "total_batches": 11_798,
                "spatial_tile_count": 5_899,
                "spatial_tile_max_cells": 10_000,
                "blockers": [],
            },
            public_source_plan_info={"shared_raster_cache_enabled": True, "blockers": []},
        )

        self.assertFalse(report["ready_for_full_scope"])
        self.assertIn(
            "Public raster-zonal Stage 4 requires observed throughput before full-scope readiness",
            report["blockers"],
        )

    def test_public_scaling_report_blocks_full_scope_without_shared_cache(self):
        report = check_stage4_public_scaling._scaling_report(
            scope_total_cells=20_953_582,
            cadence_days=10,
            hard_ceiling_cells=100_000,
            active_provider_adapter="public_raster_zonal",
            latest_probe=None,
            metric_statuses=[],
            raster_zonal_proof={"required": True, "ok": True, "latest": {"ndvi_ok_ratio": 0.94}},
            raster_zonal_plan={
                "provider_adapter": "public_raster_zonal",
                "supports_full_scope": True,
                "diagnostic_only": False,
                "batching_strategy": "stage3_spatial_tiles",
                "total_cells": 20_953_582,
                "total_batches": 11_798,
                "spatial_tile_count": 5_899,
                "spatial_tile_max_cells": 10_000,
                "blockers": [],
            },
            raster_zonal_timing={
                "completed_batches": 115,
                "failed_batches": 0,
                "processed_cells": 493_074,
                "ndvi_duration_seconds": 1800,
                "open_alerts": 0,
            },
            public_source_plan_info={"shared_raster_cache_enabled": False, "blockers": ["Shared public raster cache is disabled"]},
        )

        self.assertFalse(report["ready_for_full_scope"])
        self.assertIn("Shared public raster cache is required before full-scope public raster-zonal runs", report["blockers"])


    def test_public_scaling_report_blocks_raster_zonal_with_failed_batches(self):
        report = check_stage4_public_scaling._scaling_report(
            scope_total_cells=20_953_582,
            cadence_days=10,
            hard_ceiling_cells=100_000,
            active_provider_adapter="public_raster_zonal",
            latest_probe=None,
            metric_statuses=[],
            raster_zonal_proof={"required": True, "ok": True, "latest": {"ndvi_ok_ratio": 0.94}},
            raster_zonal_plan={
                "provider_adapter": "public_raster_zonal",
                "supports_full_scope": True,
                "diagnostic_only": False,
                "batching_strategy": "stage3_spatial_tiles",
                "total_cells": 20_953_582,
                "total_batches": 11_798,
                "spatial_tile_count": 5_899,
                "spatial_tile_max_cells": 10_000,
                "blockers": [],
            },
            raster_zonal_timing={
                "completed_batches": 115,
                "failed_batches": 1,
                "processed_cells": 493_074,
                "ndvi_duration_seconds": 1800,
                "open_alerts": 0,
            },
            public_source_plan_info={"shared_raster_cache_enabled": True, "blockers": []},
        )

        self.assertFalse(report["ready_for_full_scope"])
        self.assertIn(
            "Public raster-zonal Stage 4 has failed batches that must be repaired before full-scope readiness",
            report["blockers"],
        )

    def test_public_scaling_timing_uses_full_completed_run_with_recent_fallback(self):
        source = inspect.getsource(check_stage4_public_scaling._latest_public_raster_zonal_timing)

        self.assertIn("full_completed_run", source)
        self.assertIn("recent_completed_batches", source)
        self.assertIn("recency_rank <= 50", source)
        self.assertIn("full_first_started_at", source)
        self.assertIn("total_completed_batches", source)
        self.assertIn("failed_batches", source)
        self.assertIn("batch.finished_at <= run.finished_at", source)

    def test_stage4_plan_apply_does_not_allow_blocker_override(self):
        source = inspect.getsource(run_stage4_automation_plan)

        self.assertNotIn("allow_blockers", source)
        self.assertNotIn("--allow-blockers", source)

    def test_stage4_plan_apply_refuses_duplicate_full_runs(self):
        source = inspect.getsource(run_stage4_automation_plan)

        self.assertIn("active_stage4_run", source)
        self.assertIn("--force-new-run", source)
        self.assertIn("Resume queued or failed batches instead", source)
        self.assertIn("Use --force-new-run only for capped diagnostic runs", source)

    def test_stage4_full_run_apply_requires_provider_preflight(self):
        source = inspect.getsource(run_stage4_automation_plan)
        task_source = (Path(__file__).resolve().parents[1] / "app/tasks/stage4.py").read_text()
        preflight_source = inspect.getsource(stage4_automation.require_stage4_provider_preflight)

        self.assertIn("require_stage4_provider_preflight", source)
        self.assertIn("args.max_cells is None", source)
        self.assertIn("provider_preflight", source)
        self.assertIn("require_stage4_provider_preflight", task_source)
        self.assertIn("Stage 4 provider preflight failed", preflight_source)

    def test_stage4_resume_command_repairs_existing_run(self):
        source = inspect.getsource(resume_stage4_run)

        self.assertIn("_reset_repairable_batches", source)
        self.assertIn("_resolve_reset_alerts", source)
        self.assertIn("_clear_stage4_queue", source)
        self.assertIn("--reset-stuck", source)
        self.assertIn("--process-inline", source)
        self.assertIn("--max-batches", source)
        self.assertIn("process_batch", source)
        self.assertIn("--clear-stage4-queue", source)
        self.assertIn("load_queued_batch_ids", source)

    def test_partitioned_area_backfill_uses_active_grid_precision(self):
        source = inspect.getsource(stage4_automation._area_coefficient_backfill_sql)

        self.assertIn("active.area_coefficient", source)
        self.assertIn("active.area_ha", source)
        self.assertIn("cell.id BETWEEN :start_id AND :end_id", source)

    def test_stage4_records_failure_events(self):
        source = inspect.getsource(stage4_automation.record_event)

        self.assertIn("stage4_layer_b_events", source)
        self.assertIn("event_type", source)
        self.assertIn("severity", source)

    def test_scope_uses_effective_area_coefficient(self):
        source = inspect.getsource(stage4_automation.load_scope)

        self.assertIn("cell.area_coefficient IS NULL", source)

    def test_scheduler_treats_external_batches_as_active_work(self):
        source = inspect.getsource(stage4_automation.due_for_scheduled_run)

        self.assertIn("'external_wait'", source)
        self.assertIn("'external_ready'", source)

    def test_rollup_keeps_cancelled_runs_cancelled(self):
        source = inspect.getsource(stage4_automation.refresh_run_rollup)

        self.assertIn("cancelled_batches", source)
        self.assertIn("THEN 'cancelled'", source)
        self.assertIn("COALESCE(run.finished_at, now())", source)

    def test_full_scope_batch_statistical_requires_s3_access(self):
        scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=20_953_582,
            min_cell_id=1,
            max_cell_id=20_953_582,
            missing_area_coefficient_cells=0,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_processing_path="industrial",
                stage4_provider_adapter="industrial",
                stage4_ndvi_processing_mode="batch_statistical",
                stage4_batch_statistical_input_uri="s3://map1-stage4/input",
                stage4_batch_statistical_output_uri="s3://map1-stage4/output",
            ),
        )
        self.assertIn(
            "Batch Statistical S3 access key pair is required for input upload and output import",
            blockers,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_processing_path="industrial",
                stage4_provider_adapter="industrial",
                stage4_ndvi_processing_mode="batch_statistical",
                stage4_batch_statistical_input_uri="storagebox://map1-stage4/input",
                stage4_batch_statistical_output_uri="s3://map1-stage4/output",
                stage4_batch_statistical_s3_access_key="access-key",
                **{"stage4_batch_statistical_s3_" + "secret_access_key": "bucket-key-pair"},
            ),
        )
        self.assertIn(
            "Batch Statistical input and output roots must be s3:// object-storage URIs",
            blockers,
        )

        blockers = stage4_automation._plan_blockers(
            scope,
            Settings(
                stage4_processing_path="industrial",
                stage4_provider_adapter="industrial",
                stage4_ndvi_processing_mode="batch_statistical",
                stage4_batch_statistical_input_uri="s3://map1-stage4/input",
                stage4_batch_statistical_output_uri="s3://map1-stage4/output",
                stage4_batch_statistical_s3_iam_role_arn="arn:aws:iam::123456789012:role/map1-stage4",
                stage4_batch_statistical_s3_access_key="access-key",
                **{"stage4_batch_statistical_s3_" + "secret_access_key": "bucket-key-pair"},
            ),
        )
        self.assertEqual(blockers, [])

    def test_public_stack_blocks_full_scope_until_explicit_scaling(self):
        full_scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=20_953_582,
            min_cell_id=1,
            max_cell_id=20_953_582,
            missing_area_coefficient_cells=0,
        )

        blockers = stage4_automation._plan_blockers(
            full_scope,
            Settings(
                stage4_provider_adapter="public_statistics",
                stage4_ndvi_processing_mode="public_statistics",
            ),
        )

        self.assertIn(
            "Public statistics path is limited to proof runs until full-scope scaling is explicitly enabled",
            blockers,
        )
        self.assertNotIn(
            "Batch Statistical object storage URI is required for full-scope Stage 4 NDVI automation",
            blockers,
        )

    def test_public_stack_proof_does_not_require_s3(self):
        proof_scope = Stage4Scope(
            region_id=1,
            region_code="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10",
            grid_version="stage3-public-ogm-osm-union-clean-v10",
            total_cells=10,
            min_cell_id=1,
            max_cell_id=10,
            missing_area_coefficient_cells=0,
        )

        self.assertEqual(stage4_automation._plan_blockers(proof_scope, Settings()), [])

    def test_batch_statistical_submit_uploads_input_before_provider_request(self):
        source = inspect.getsource(stage4_automation.submit_batch_statistical_ndvi_batch)

        self.assertLess(source.index("prepare_batch_statistical_input"), source.index("get_access_token"))
        self.assertIn("batch_statistical_input_upload", source)

    def test_batch_statistical_poll_terminal_failures_do_not_loop_forever(self):
        source = inspect.getsource(stage4_automation.poll_batch_statistical_requests)

        self.assertIn("failed_external", source)
        self.assertIn("finished_at = CASE WHEN :failed_external THEN now()", source)
        self.assertIn("batch_statistical_failed", source)

    def test_batch_statistical_poll_partial_is_ready_not_stuck(self):
        source = inspect.getsource(stage4_automation.poll_batch_statistical_requests)

        self.assertIn('normalized_status == "PARTIAL"', source)
        self.assertIn('normalized_status in {"DONE", "PARTIAL"}', source)
        self.assertIn("batch_statistical_partial", source)

    def test_batch_statistical_status_metadata_is_redacted(self):
        status_info = {
            "status": "DONE",
            "request": {
                "input": {"features": {"s3": {"accessKey": "access-key", "secretAccessKey": "bucket-key-pair"}}}
            },
        }

        redacted = stage4_automation._redact_sensitive_metadata(status_info)

        self.assertEqual(redacted["request"]["input"]["features"]["s3"]["accessKey"], "[redacted]")
        self.assertEqual(redacted["request"]["input"]["features"]["s3"]["secretAccessKey"], "[redacted]")

    def test_stage4_json_redacts_sensitive_metadata_at_write_boundary(self):
        payload = stage4_automation._json(
            {
                "source": {
                    "token": "raw-token",
                    "signed_url": "https://example.test/?signature=raw",
                    "safe": "visible",
                }
            }
        )

        self.assertIn('"token":"[redacted]"', payload)
        self.assertIn('"signed_url":"[redacted]"', payload)
        self.assertIn('"safe":"visible"', payload)
        self.assertNotIn("raw-token", payload)

    def test_public_stac_all_scene_read_errors_trip_source_unavailable_circuit(self):
        self.assertTrue(
            stage4_automation._is_stage4_source_unavailable(
                RuntimeError("Public STAC COG scenes were found but none could be read")
            )
        )

    def test_public_stac_asset_read_failure_defers_batch_without_substituting_scene(self):
        from app.services.stage4_public_cog import PublicStacAssetReadError

        self.assertTrue(stage4_automation._is_stage4_source_unavailable(
            PublicStacAssetReadError("Public STAC COG asset could not be read; retry the batch")
        ))

    def test_stage4_celery_requeues_lost_worker_tasks(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "services" / "stage4_celery.py").read_text()

        self.assertIn("task_reject_on_worker_lost=True", source)
        self.assertIn("task_soft_time_limit", source)
        self.assertIn("task_time_limit", source)

    def test_public_raster_cache_manifest_tracks_checksum(self):
        from app.services import stage4_public_sources

        source = inspect.getsource(stage4_public_sources)

        self.assertIn('"sha256"', source)
        self.assertIn("_file_sha256", source)
        self.assertIn("expected_sha256", source)

    def test_cached_raster_preserves_scene_provenance_without_fetching_again(self):
        from app.services import stage4_public_sources as sources

        provenance = {
            "scene_count": 1,
            "selected_scene_ids": ["test-scene"],
            "selected_scene_datetimes": ["2026-10-09T08:38:35Z"],
            "collection": "sentinel-2-l2a",
            "mosaic": {"scenes_used": 1},
        }
        for from_file in (False, True):
            with self.subTest(from_file=from_file), TemporaryDirectory() as tmp_dir:
                settings = Settings(stage4_public_raster_cache_enabled=True, stage4_public_raster_cache_dir=tmp_dir)
                identity = sources.public_raster_cache_identity(
                    run={"requested_start": "2026-09-17", "requested_end": "2026-11-01", "max_cloud_coverage": 40},
                    batch={}, settings=settings, bbox=[0, 0, 10, 10], crs_epsg=6933, width=1, height=1,
                )
                original = Path(tmp_dir) / "source.tif"
                original.write_bytes(b"validated-raster-fixture")
                writer = sources.write_public_raster_cache_file if from_file else sources.write_public_raster_cache
                raster = writer(settings, identity, original if from_file else original.read_bytes(), lambda path: None, source_metadata=provenance)
                self.assertEqual(sources.load_valid_public_raster_cache(settings, identity), raster)
                with patch.object(stage4_automation, "_validate_public_raster_zonal_tiff"), patch("requests.post", side_effect=AssertionError("Unexpected source fetch")):
                    warm = stage4_automation._public_raster_zonal_cache_hit_result(
                        raster, identity=identity, settings=settings, source_adapter="public_stac_cog",
                        target_mask_strategy="full_raster", target_mask_sha256=None,
                    )
                self.assertTrue(warm["cache_hit"])
                self.assertEqual(warm["source_metadata"]["selected_scene_ids"], provenance["selected_scene_ids"])
                self.assertEqual(warm["source_metadata"]["selected_scene_datetimes"], provenance["selected_scene_datetimes"])
                self.assertEqual(warm["source_metadata"]["mosaic"], provenance["mosaic"])
                old_identity = {**identity, "source_algorithm": "public-stac-cog-shared-source-tile-mosaic-v8"}
                self.assertIsNone(sources.load_valid_public_raster_cache(settings, old_identity))


class Stage4ResultProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = {
            "id": 1, "run_uuid": "run-one", "region_id": 1,
            "region_code": "TR-PROOF", "grid_version": "proof",
            "requested_start": date(2026, 9, 17), "requested_end": date(2026, 11, 3),
            "max_cloud_coverage": 40, "min_valid_pixel_ratio": 0.35,
        }
        self.batch = {
            "id": 7, "batch_index": 1, "task_name": "ndvi_update",
            "metadata": {"bbox_projected": [0, 0, 20, 20], "projected_srid": 6933,
                         "stage3_grid_run_id": 1, "stage3_grid_tile_id": 1},
        }
        self.settings = Settings(
            stage4_public_raster_zonal_results_dir=str(self.root / "results"),
            stage4_work_dir=str(self.root / "work"),
            stage4_public_raster_cache_dir=str(self.root / "cache"),
            stage4_public_raster_cache_enabled=True,
            stage4_public_raster_zonal_source_tile_size_m=20,
            stage4_public_raster_zonal_partitioned_mirror_enabled=False,
        )
        self.path = stage4_automation._public_raster_zonal_result_output_path(
            self.run, self.batch, self.settings,
        )
        self.row = {
            "cell_id": 1, "metric_date": "2026-11-03", "ndvi": "0.500000",
            "source_status": "ok", "valid_pixel_ratio": "1.000000",
            "sample_count": 4, "valid_pixel_count": 4,
            "source_product_id": "same-date-same-batch-product",
        }

    def _publish(self, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        stage4_automation._write_public_raster_zonal_csv(path, [self.row])
        stage4_automation._write_public_raster_zonal_manifest(
            path, run=self.run, batch=self.batch, settings=self.settings,
            bbox=[0, 0, 20, 20], crs_epsg=6933, row_count=1,
        )
        return path

    def test_csv_resume_rejects_changed_source_settings_on_both_lookup_paths(self):
        changes = {
            "stage4_public_stac_api_url": "https://alternate.invalid/search",
            "stage4_public_stac_collection": "different-collection",
            "stage4_public_stac_scene_limit": 48,
            "stage4_public_raster_zonal_source_tile_size_m": 40,
            "stage4_public_raster_zonal_resolution_m": 20,
            "stage4_public_source_adapter": "cdse_process",
        }
        legacy_path = Path(self.settings.stage4_public_raster_zonal_results_dir) / self.path.name
        for legacy in (False, True):
            path = self._publish(legacy_path if legacy else self.path)
            for setting, value in changes.items():
                for reuse in (False, True) if not legacy else (True,):
                    with self.subTest(legacy=legacy, setting=setting, reuse=reuse):
                        settings = self.settings.model_copy(update={
                            setting: value, "stage4_public_raster_zonal_reuse_existing_csv": reuse,
                        })
                        with patch.object(stage4_automation, "_generate_public_raster_zonal_results") as generate:
                            with self.assertRaisesRegex(RuntimeError, "does not match current run.*clean run"):
                                stage4_automation.ensure_public_raster_zonal_results(self.run, self.batch, settings)
                            generate.assert_not_called()
            path.unlink()
            stage4_automation._public_raster_zonal_manifest_path(path).unlink()

    def test_csv_resume_checks_checksum_row_count_and_source_cache_key(self):
        for damage in ("bytes", "row_count", "source_cache_key", "source_identity"):
            with self.subTest(damage=damage):
                self._publish()
                manifest = stage4_automation._public_raster_zonal_manifest_path(self.path)
                payload = json.loads(manifest.read_text())
                if damage == "bytes":
                    self.path.write_text(self.path.read_text().replace("0.500000", "0.900000"))
                elif damage == "row_count":
                    payload[damage] = 2
                else:
                    payload.pop(damage)
                manifest.write_text(json.dumps(payload))
                with patch.object(stage4_automation, "_generate_public_raster_zonal_results") as generate:
                    with self.assertRaises(RuntimeError):
                        stage4_automation.ensure_public_raster_zonal_results(self.run, self.batch, self.settings)
                    generate.assert_not_called()

    def test_legacy_csv_without_manifest_is_never_trusted_for_either_adapter(self):
        legacy = Path(self.settings.stage4_public_raster_zonal_results_dir) / self.path.name
        self._publish(legacy)
        stage4_automation._public_raster_zonal_manifest_path(legacy).unlink()
        for adapter in ("public_stac_cog", "cdse_process"):
            with self.subTest(adapter=adapter):
                settings = self.settings.model_copy(update={
                    "stage4_public_source_adapter": adapter,
                    "stage4_public_raster_zonal_reuse_existing_csv": True,
                })
                with self.assertRaisesRegex(RuntimeError, "manifest is missing"):
                    stage4_automation.ensure_public_raster_zonal_results(self.run, self.batch, settings)
                self.assertTrue(legacy.is_file())

    def test_manifest_writer_rejects_different_actual_source_key(self):
        self._publish()
        with self.assertRaisesRegex(RuntimeError, "source does not match manifest identity"):
            stage4_automation._write_public_raster_zonal_manifest(
                self.path, run=self.run, batch=self.batch, settings=self.settings,
                bbox=[0, 0, 20, 20], crs_epsg=6933, row_count=1,
                source_raster={"cache_key": "wrong", "source_adapter": "public_stac_cog"},
            )

    def test_interrupted_csv_publication_recovers_from_valid_shared_raster(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds
        from rasterio.warp import transform_geom
        from app.services import stage4_public_sources as sources

        original = self.root / "source.tif"
        with rasterio.open(original, "w", driver="GTiff", width=2, height=2, count=2,
                           dtype="float32", crs="EPSG:6933",
                           transform=from_bounds(0, 0, 20, 20, 2, 2)) as raster:
            raster.write(np.full((2, 2), 0.5, dtype="float32"), 1)
            raster.write(np.ones((2, 2), dtype="float32"), 2)
        identity = stage4_automation._public_raster_source_identity(
            run=self.run, batch=self.batch, settings=self.settings,
            bbox=[0, 0, 20, 20], crs_epsg=6933, width=2, height=2,
        )
        sources.write_public_raster_cache_file(
            self.settings, identity, original, stage4_automation._validate_public_raster_zonal_tiff,
            source_metadata={"selected_scene_ids": ["frozen-scene"], "scene_count": 1},
        )
        geometry = transform_geom("EPSG:6933", "EPSG:4326", {
            "type": "Polygon", "coordinates": [[[0, 0], [20, 0], [20, 20], [0, 20], [0, 0]]],
        })
        real_csv_writer = stage4_automation._write_public_raster_zonal_csv
        real_manifest_writer = stage4_automation._write_public_raster_zonal_manifest
        manifest_path = stage4_automation._public_raster_zonal_manifest_path(self.path)

        for boundary in ("before_csv_rename", "after_csv_rename", "before_manifest_rename",
                         "after_manifest_rename", "manifest_without_csv"):
            with self.subTest(boundary=boundary):
                self.path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)

                def interrupt_csv(path, rows):
                    if boundary == "before_csv_rename":
                        path.with_suffix(path.suffix + ".tmp").write_text("partial")
                    else:
                        real_csv_writer(path, rows)
                    raise RuntimeError("simulated interruption")

                def interrupt_manifest(path, **kwargs):
                    if boundary == "before_manifest_rename":
                        manifest_path.with_suffix(manifest_path.suffix + ".tmp").write_text("{")
                    else:
                        real_manifest_writer(path, **kwargs)
                    raise RuntimeError("simulated interruption")

                with patch.object(stage4_automation, "SessionLocal"), \
                     patch.object(stage4_automation, "_load_batch_cells", return_value=[{"id": 1, "geometry": geometry}]), \
                     patch.object(stage4_automation, "_record_public_raster_zonal_generation_artifacts"), \
                     patch.object(stage4_public_cog, "build_public_stac_ndvi_raster", side_effect=AssertionError("Unexpected source fetch")):
                    target = "_write_public_raster_zonal_csv" if "csv_rename" in boundary else "_write_public_raster_zonal_manifest"
                    effect = interrupt_csv if "csv_rename" in boundary else interrupt_manifest
                    with patch.object(stage4_automation, target, side_effect=effect):
                        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                            stage4_automation.ensure_public_raster_zonal_results(self.run, self.batch, self.settings)
                    if boundary == "manifest_without_csv":
                        self.path.unlink()
                    if boundary == "after_csv_rename":
                        # A plausible but incorrect orphan must never be adopted.
                        self.path.write_text(self.path.read_text().replace("0.500000", "0.900000"))
                    result = stage4_automation.ensure_public_raster_zonal_results(self.run, self.batch, self.settings)
                    stage4_automation._validate_public_raster_zonal_manifest(
                        result, run=self.run, batch=self.batch, settings=self.settings,
                    )
                    rows = stage4_automation._read_public_raster_zonal_rows(result)
                    self.assertEqual(rows[0]["ndvi"], "0.500000")
                    self.assertIn("run-run-one", rows[0]["source_product_id"])
                    self.assertIn(public_raster_cache_key(identity), rows[0]["source_product_id"])
                    manifest = json.loads(manifest_path.read_text())
                    self.assertEqual(manifest["source_identity"], identity)
                    self.assertEqual(manifest["source"]["selected_scene_ids"], ["frozen-scene"])
                    # Once published, retry needs neither raster nor database access.
                    with patch.object(stage4_automation, "_generate_public_raster_zonal_results") as generate:
                        self.assertEqual(stage4_automation.ensure_public_raster_zonal_results(
                            self.run, self.batch, self.settings), result)
                        generate.assert_not_called()

    def test_all_metric_writers_preserve_previous_run_and_retry_idempotently(self):
        # Execute each writer's actual INSERT/conflict action in SQLite. PostgreSQL
        # source CTEs are supplied as fixture tables; casts/functions are adapted.
        for writer in ("raster", "biomass", "statistics"):
            with self.subTest(writer=writer), sqlite3.connect(":memory:") as conn:
                conn.create_function("now", 0, lambda: "2026-11-03T00:00:00")
                conn.create_function("jsonb_build_object", -1, lambda *args: json.dumps(dict(zip(args[::2], args[1::2]))))
                conn.executescript("""
                    CREATE TABLE stage4_layer_b_metrics (
                        id INTEGER PRIMARY KEY, run_id INTEGER, batch_id INTEGER,
                        grid_cell_id INTEGER, region_id INTEGER, region_code TEXT,
                        grid_version TEXT, metric_name TEXT, metric_date TEXT,
                        value REAL, per_hectare_value REAL, area_coefficient REAL,
                        area_weighted_value REAL, unit TEXT, source_status TEXT,
                        source_provider TEXT, source_product_id TEXT, quality_score REAL,
                        metadata TEXT, updated_at TEXT,
                        UNIQUE(grid_cell_id, metric_name, metric_date, source_provider, source_product_id)
                    );
                    CREATE TABLE source_rows (
                        grid_cell_id INTEGER, acquisition_date TEXT, ndvi REAL,
                        area_coefficient REAL, source_status TEXT, source_product_id TEXT,
                        valid_pixel_ratio REAL, observation_id INTEGER
                    );
                    CREATE TABLE calculated (
                        grid_cell_id INTEGER, metric_date TEXT, metric_value REAL,
                        area_coefficient REAL, source_status TEXT, quality_score REAL
                    );
                """)

                def import_metric(run, value):
                    db = MagicMock()
                    db.execute.return_value.mappings.return_value.one.return_value = {
                        "processed_cells": 1, "succeeded_cells": 1, "failed_cells": 0,
                        "skipped_cells": 0, "extra_rows": 0,
                    }
                    with patch.object(stage4_automation, "_batch_cells_sql", return_value=("SELECT 1", {})):
                        if writer == "raster":
                            stage4_automation._bulk_import_public_raster_zonal_rows(
                                db, run=run, batch=self.batch, results_path=self.path, settings=self.settings,
                            )
                        elif writer == "biomass":
                            stage4_automation._bulk_import_biomass_rows(db, run=run, batch=self.batch, settings=self.settings)
                        else:
                            stage4_automation.record_layer_b_metric(
                                db, run_id=run["id"], batch_id=7, grid_cell_id=1, region_id=1,
                                region_code="TR-PROOF", grid_version="proof", metric_name="ndvi",
                                metric_date=run["requested_end"], value=value, unit="index",
                                source_status="ok", source_provider="cdse_public_statistics_api",
                                source_product_id=self.row["source_product_id"], area_coefficient=1,
                                quality_score=1, metadata={},
                            )
                    statements = [call.args for call in db.execute.call_args_list
                                  if "INSERT INTO stage4_layer_b_metrics" in str(call.args[0])]
                    self.assertEqual(len(statements), 1)
                    statement, params = statements[0]
                    sql = str(statement)
                    sql = sql[sql.index("INSERT INTO stage4_layer_b_metrics"):]
                    sql = re.sub(r"::numeric\b", "", sql)
                    sql = re.sub(r"CAST\((:[a-z_]+) AS (?:jsonb|date)\)", r"\1", sql)
                    sql = re.sub(r"(FROM (?:source_rows source|calculated))\s+ON CONFLICT", r"\1 WHERE true ON CONFLICT", sql)
                    params = {key: float(val) if isinstance(val, Decimal) else str(val) if isinstance(val, date) else val
                              for key, val in params.items()}
                    conn.execute("DELETE FROM source_rows")
                    conn.execute("INSERT INTO source_rows VALUES (1, '2026-11-03', ?, 1, 'ok', ?, 1, 10)",
                                 (value, self.row["source_product_id"]))
                    conn.execute("DELETE FROM calculated")
                    conn.execute("INSERT INTO calculated VALUES (1, '2026-11-03', ?, 1, 'ok', 1)", (value,))
                    conn.execute(sql, params)

                import_metric(self.run, 0.8)
                before = conn.execute("SELECT * FROM stage4_layer_b_metrics WHERE run_id=1").fetchall()
                replay = {**self.run, "id": 2, "run_uuid": "proof-run", "requested_start": date(2026, 10, 1)}
                import_metric(replay, 0.5)
                import_metric(replay, 0.5)
                self.assertEqual(conn.execute("SELECT * FROM stage4_layer_b_metrics WHERE run_id=1").fetchall(), before)
                self.assertEqual(conn.execute("SELECT run_id, value FROM stage4_layer_b_metrics ORDER BY run_id").fetchall(),
                                 [(1, 0.8), (2, 0.5)])


class Stage4BatchLeaseTest(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine, event, text
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{self.tmp.name}/leases.db", poolclass=NullPool)
        self.addCleanup(self.engine.dispose)
        self.locks = {}
        self.opened = 0

        @event.listens_for(self.engine, "connect")
        def configure(connection, _record):
            self.opened += 1

            def acquire(key):
                if key in self.locks and self.locks[key] is not connection:
                    return False
                self.locks[key] = connection
                return True

            def release(key):
                if self.locks.get(key) is not connection:
                    return False
                del self.locks[key]
                return True

            connection.create_function("hashtextextended", 2, lambda value, seed: int(value.rsplit(":", 1)[1]))
            connection.create_function("pg_try_advisory_lock", 1, acquire)
            connection.create_function("pg_advisory_unlock", 1, release)

        @event.listens_for(self.engine, "close")
        def release_closed(connection, _record):
            for key in list(self.locks):
                if self.locks[key] is connection:
                    del self.locks[key]

        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE mutations (value INTEGER)"))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(stage4_automation, "engine", self.engine))
        self.stack.enter_context(patch.object(stage4_automation, "SessionLocal", sessionmaker(bind=self.engine)))

    def test_commits_preserve_lock_and_all_processing_sessions_use_owner(self):
        from sqlalchemy import text

        with stage4_automation._stage4_batch_execution_lock(7) as lease:
            for value in (1, 2):
                with stage4_automation._stage4_batch_session() as db:
                    self.assertIs(db.get_bind(), lease.connection)
                    db.execute(text("INSERT INTO mutations VALUES (:value)"), {"value": value})
                    db.commit()
            self.assertIs(self.locks[7], lease.driver_connection)
            with patch.object(stage4_automation, "get_stage4_ndvi_provider") as provider:
                self.assertEqual(stage4_automation.process_batch(7)["status"], "batch_busy")
                provider.assert_not_called()
        self.assertEqual(self.locks, {})
        self.assertIsNone(stage4_automation._stage4_batch_lease.get())
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT value FROM mutations ORDER BY value")).scalars().all(), [1, 2])
        with stage4_automation._stage4_batch_execution_lock(7) as lease:
            self.assertIsNotNone(lease)

    def test_lost_connection_blocks_new_sessions_retained_sessions_and_commit(self):
        from sqlalchemy import text

        for boundary in ("new_session", "retained_session", "commit", "raw_sql"):
            with self.subTest(boundary=boundary):
                with stage4_automation._stage4_batch_execution_lock(7) as lease:
                    with stage4_automation._stage4_batch_session() as db:
                        db.execute(text("SELECT 1"))
                        if boundary != "commit":
                            db.commit()
                        lease.connection.invalidate()
                        opened = self.opened
                        with self.assertRaises(stage4_automation.Stage4BatchLeaseLost):
                            if boundary == "new_session":
                                with stage4_automation._stage4_batch_session():
                                    self.fail("Lost worker opened a new session")
                            elif boundary == "retained_session":
                                db.execute(text("INSERT INTO mutations VALUES (99)"))
                            elif boundary == "commit":
                                db.commit()
                            else:
                                lease.connection.exec_driver_sql("INSERT INTO mutations VALUES (99)")
                        if boundary != "raw_sql":
                            self.assertEqual(self.opened, opened)
                self.assertEqual(self.locks, {})
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM mutations")).scalar_one(), 0)

    def test_claim_requires_owned_connection_to_recover_running_or_stuck(self):
        for status in ("running", "stuck", "completed"):
            with self.subTest(status=status):
                db = MagicMock()
                db.execute.return_value.mappings.return_value.one_or_none.return_value = {
                    "id": 7, "run_id": 1, "status": status, "attempts": 1,
                }
                db.execute.return_value.scalar_one.return_value = 2
                self.assertIsNone(stage4_automation._claim_batch(db, 7))
                with stage4_automation._stage4_batch_execution_lock(7) as lease, \
                     patch.object(stage4_automation, "record_heartbeat"), \
                     patch.object(stage4_automation, "refresh_run_rollup"):
                    db.get_bind.return_value = lease.connection
                    result = stage4_automation._claim_batch(db, 7)
                    if status == "completed":
                        self.assertIsNone(result)
                    else:
                        self.assertEqual(result["attempts"], 2)
                    db.get_bind.return_value = self.engine
                    with self.assertRaises(stage4_automation.Stage4BatchLeaseLost):
                        stage4_automation._claim_batch(db, 7)

    def _stub_processing(self, stack, provider):
        stack.enter_context(patch.object(stage4_automation, "_source_unavailable_circuit_open", return_value=False))
        stack.enter_context(patch.object(stage4_automation, "_batch_waits_for_ndvi", return_value=False))
        stack.enter_context(patch.object(stage4_automation, "_claim_batch", return_value={
            "id": 7, "run_id": 1, "task_name": "ndvi_update", "expected_cells": 1,
        }))
        stack.enter_context(patch.object(stage4_automation, "_load_run", return_value={"id": 1}))
        stack.enter_context(patch.object(stage4_automation, "validate_public_statistics_batch_limits"))
        stack.enter_context(patch.object(stage4_automation, "validate_public_raster_zonal_batch_limits"))
        stack.enter_context(patch.object(stage4_automation, "get_stage4_ndvi_provider", return_value=SimpleNamespace(
            capabilities=SimpleNamespace(uses_external_jobs=False), process_batch=provider,
        )))

    def test_process_retries_lost_connection_before_import_and_finish_without_mutation(self):
        from sqlalchemy import text

        for boundary in ("import", "finish", "disconnect_during_finish"):
            with self.subTest(boundary=boundary), ExitStack() as stack:
                def provider(*_args):
                    lease = stage4_automation._stage4_batch_lease.get()
                    if boundary == "disconnect_during_finish":
                        # Simulate an idle socket closed by the server; SQLAlchemy
                        # discovers the loss only on the first finalization SQL.
                        lease.driver_connection.close()
                    else:
                        lease.connection.invalidate()
                    if boundary == "import":
                        with stage4_automation._stage4_batch_session() as db:
                            db.execute(text("INSERT INTO mutations VALUES (99)"))
                            db.commit()
                    return stage4_automation.Stage4BatchResult(1, 1, 0, 0)

                self._stub_processing(stack, provider)
                finish = stack.enter_context(patch.object(stage4_automation, "_finish_batch", side_effect=lambda db, *_args:
                    db.execute(text("INSERT INTO mutations VALUES (99)"))))
                alerts = stack.enter_context(patch.object(stage4_automation, "upsert_alert"))
                result = stage4_automation.process_batch(7, Settings())
                self.assertEqual(result["status"], "lease_lost")
                if boundary != "disconnect_during_finish":
                    finish.assert_not_called()
                alerts.assert_not_called()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM mutations")).scalar_one(), 0)

    def test_completed_redelivery_skips_provider(self):
        with ExitStack() as stack:
            provider = MagicMock()
            self._stub_processing(stack, provider)
            stack.enter_context(patch.object(stage4_automation, "_claim_batch", return_value=None))
            self.assertEqual(stage4_automation.process_batch(7, Settings())["status"], "not_found_or_terminal")
            provider.assert_not_called()

    def test_task_retries_busy_and_lost_deliveries(self):
        from app.tasks import stage4 as tasks
        from celery.exceptions import Retry

        for status in ("batch_busy", "lease_lost"):
            with self.subTest(status=status), \
                 patch.object(tasks, "process_batch", return_value={"batch_id": 7, "status": status}), \
                 patch.object(tasks.process_stage4_batch, "retry", side_effect=Retry()) as retry:
                with self.assertRaises(Retry):
                    tasks.process_stage4_batch.run(7)
                retry.assert_called_once_with(countdown=60, max_retries=None)

    def test_waiting_tasks_survive_more_than_three_celery_retries(self):
        from app.tasks import stage4 as tasks
        from celery.exceptions import Retry

        task = tasks.process_stage4_batch
        settings = Settings()
        for status in ("waiting_for_ndvi", "batch_busy", "lease_lost", "source_unavailable"):
            with self.subTest(status=status), \
                 patch.object(tasks, "get_settings", return_value=settings), \
                 patch.object(tasks, "process_batch", return_value={"batch_id": 7, "status": status}):
                task.push_request(id="waiting-batch-regression", args=(7,), kwargs={},
                                  retries=5, called_directly=False, is_eager=True)
                try:
                    # Exercise Celery's actual retry limit, without a broker.
                    with self.assertRaises(Retry) as caught:
                        task.run(7)
                    self.assertEqual(caught.exception.sig.options["retries"], 6)
                    expected_delay = settings.stage4_source_unavailable_retry_seconds if status == "source_unavailable" else 60
                    self.assertEqual(caught.exception.when, expected_delay)
                finally:
                    task.pop_request()

    def test_download_thread_heartbeat_cannot_open_an_unleased_session(self):
        from concurrent.futures import ThreadPoolExecutor

        with stage4_automation._stage4_batch_execution_lock(7) as lease:
            callback = stage4_automation._stage4_batch_heartbeat_callback(7)
            opened = self.opened
            with patch.object(stage4_automation, "_heartbeat_batch") as heartbeat:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertIsNone(pool.submit(stage4_automation._stage4_batch_lease.get).result())
                    pool.submit(callback).result()
                heartbeat.assert_not_called()
                self.assertEqual(self.opened, opened)
                callback()
                heartbeat.assert_called_once()
                self.assertIs(heartbeat.call_args.args[0].get_bind(), lease.connection)
        with patch.object(stage4_automation, "_heartbeat_batch") as heartbeat:
            with self.assertRaises(stage4_automation.Stage4BatchLeaseLost):
                callback()
            heartbeat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
