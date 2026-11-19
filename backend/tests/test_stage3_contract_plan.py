import shlex
from types import SimpleNamespace
import unittest

from app.commands.run_stage3_contract_plan import (
    _commands,
    _expected_region_grid_cells,
    _forest_region_command,
    _load_region,
    _plan_projected_srid,
    _selected_grid_tile_count,
    _selected_projected_srid,
    estimate_batches,
    estimate_grid_cells,
    estimate_grid_tiles,
    estimate_ogm_tiles,
    execution_phases,
    readiness_notes,
)


class EmptyResult:
    def mappings(self):
        return self

    def first(self):
        return None


class EmptyDb:
    def execute(self, *_args, **_kwargs):
        return EmptyResult()


class RecordingResult:
    def mappings(self):
        return self

    def first(self):
        return {
            "code": "TR",
            "name": "Turkey",
            "country_code": "TR",
            "forest_only": True,
            "region_source": "forest_mask_layer_a",
            "source_region_code": "TR-SEED",
            "area_ha": 533.0,
            "min_lon": 30.0,
            "min_lat": 40.0,
            "max_lon": 31.0,
            "max_lat": 41.0,
            "centroid_lon": 30.5,
            "centroid_lat": 40.5,
            "projected_width_m": 10000.0,
            "projected_height_m": 10000.0,
        }


class RecordingDb:
    def __init__(self):
        self.sql = ""

    def execute(self, statement, *_args, **_kwargs):
        self.sql = str(statement)
        return RecordingResult()


class Stage3ContractPlanTest(unittest.TestCase):
    def test_small_and_full_mask_commands_pin_the_same_geometry_settings(self):
        settings = {
            "--projected-srid": "6933", "--tile-size-m": "10000",
            "--snap-grid-m": "0", "--edge-tolerance-m": "0.02",
            "--subdivide-max-vertices": "256", "--min-mask-component-area-ha": "1.0",
        }
        for public_scope, region in ((True, {"area_ha": 3055.32}), (False, None)):
            with self.subTest(public_scope=public_scope):
                command = _forest_region_command(
                    SimpleNamespace(region_code="TR-PROOF", ack_full_turkey_user_approved=False),
                    public_scope, region, source_region_code="TR-SOURCE", projected_srid=6933,
                )
                tokens = shlex.split(command)
                self.assertNotIn("--ack-full-turkey-user-approved", tokens)
                for flag, value in settings.items():
                    self.assertEqual(tokens[tokens.index(flag) + 1], value)

    def test_load_region_reports_missing_region_without_traceback(self):
        with self.assertRaisesRegex(SystemExit, "region not found: TR-MISSING"):
            _load_region(EmptyDb(), "TR-MISSING")

    def test_load_region_carries_region_metadata_through_plan_query(self):
        db = RecordingDb()

        region = _load_region(db, "TR")

        self.assertTrue(region["forest_only"])
        self.assertEqual(region["source_region_code"], "TR-SEED")
        self.assertIn("metadata,", db.sql)
        self.assertIn("metadata->>'stage3_region_source'", db.sql)
        self.assertIn("source_region_code", db.sql)

    def test_estimates_grid_cells_from_area_and_cell_size(self):
        self.assertEqual(estimate_grid_cells(23_000_000, 100), 23_000_000)
        self.assertEqual(estimate_grid_cells(10.1, 100), 11)
        self.assertEqual(estimate_grid_cells(4, 200), 1)

    def test_estimates_tile_counts(self):
        self.assertEqual(estimate_grid_tiles(width_m=10_000, height_m=10_000, tile_size_m=10_000), 1)
        self.assertEqual(estimate_grid_tiles(width_m=10_001, height_m=10_000, tile_size_m=10_000), 2)
        self.assertEqual(estimate_ogm_tiles(30, 40, 30.3, 40.3, 0.1), 9)
        self.assertEqual(estimate_ogm_tiles(30, 40, 30, 40, 0.1), 1)

    def test_estimates_batches(self):
        self.assertEqual(estimate_batches(0, 100), 0)
        self.assertEqual(estimate_batches(100, 100), 1)
        self.assertEqual(estimate_batches(101, 100), 2)

    def test_public_full_scale_seed_uses_contract_target_before_forest_refine(self):
        estimate = _expected_region_grid_cells(
            current={"latest_completed_grid_cells": 0},
            region={"forest_only": False},
            area_based_estimate=136_307_693,
            acceptance_scope="public-ogm",
            full_scale_target=23_000_000,
        )

        self.assertEqual(estimate, 23_000_000)

    def test_public_full_scale_seed_uses_equal_area_projection(self):
        class Args:
            acceptance_scope = "public-ogm"
            target_hectares = 23_000_000
            full_scale_projected_srid = 6933

        projected_srid = _plan_projected_srid(
            Args(),
            {
                "forest_only": False,
                "centroid_lon": 31.0,
                "centroid_lat": 40.0,
            },
        )

        self.assertEqual(projected_srid, 6933)

    def test_existing_grid_run_projection_and_tile_count_take_precedence(self):
        class Args:
            acceptance_scope = "public-ogm"
            target_hectares = 23_000_000
            full_scale_projected_srid = 6933
            grid_size_m = 100
            grid_tile_size_m = 10_000

        region = {
            "forest_only": True,
            "centroid_lon": 31.0,
            "centroid_lat": 40.0,
            "projected_width_m": 100_000,
            "projected_height_m": 100_000,
        }
        current = {"latest_grid_run": {"projected_srid": 6933, "total_tiles": 13720}}

        self.assertEqual(_selected_projected_srid(Args(), region, current), 6933)
        self.assertEqual(
            _selected_grid_tile_count(Args(), region, current, target_grid_cells=23_000_000),
            13720,
        )

    def test_execution_phases_include_stage3_contract_controls(self):
        phases = execution_phases(
            grid_batches=12,
            target_grid_batches=92,
            grid_tile_count=40,
            ogm_tile_count=18,
        )

        names = [phase["name"] for phase in phases]
        self.assertEqual(names[0], "source_access_check")
        self.assertLess(names.index("layer_a_ingest"), names.index("forest_region_refine"))
        self.assertLess(names.index("osm_forest_mask"), names.index("forest_region_refine"))
        self.assertLess(names.index("forest_region_refine"), names.index("grid_load"))
        self.assertLess(names.index("grid_load"), names.index("forest_clip_check"))
        self.assertIn("grid_load", names)
        self.assertIn("layer_a_ingest", names)
        self.assertIn("osm_forest_mask", names)
        self.assertIn("forest_clip_check", names)
        self.assertIn("clean_mask_integrity", names)
        self.assertIn("mask_formula_integrity", names)
        self.assertIn("grid_spatial_integrity", names)
        self.assertLess(names.index("forest_clip_check"), names.index("clean_mask_integrity"))
        self.assertLess(names.index("clean_mask_integrity"), names.index("mask_formula_integrity"))
        self.assertLess(names.index("mask_formula_integrity"), names.index("grid_spatial_integrity"))
        self.assertIn("region_seed", names)
        self.assertIn("forest_region_refine", names)
        self.assertIn("postgis_optimization", names)
        self.assertEqual(next(phase for phase in phases if phase["name"] == "grid_plan")["estimated_tiles"], 40)
        self.assertEqual(next(phase for phase in phases if phase["name"] == "layer_a_ingest")["estimated_tiles"], 18)

    def test_commands_keep_scope_and_performance_report_link(self):
        class Args:
            region_code = "TR_BOLU_TEST"
            grid_version = "stage3-v1"
            grid_size_m = 100
            grid_tile_size_m = 10000
            ogm_tile_size_deg = 0.12
            target_hectares = 23_000_000
            expected_layer_a_features = 12
            acceptance_scope = "public-ogm"

        region = {
            "min_lon": 30.0,
            "min_lat": 40.0,
            "max_lon": 31.0,
            "max_lat": 41.0,
            "area_ha": 533.0,
            "source_region_code": "TR_PUBLIC_SOURCE",
        }
        commands = _commands(Args(), projected_srid=32636, region_grid_cells=533, region=region)

        self.assertIn("--region-code TR_PUBLIC_SOURCE", commands["seed_region"])
        self.assertIn("--bbox 30,40,31,41", commands["seed_region"])
        self.assertNotIn("--turkey-bbox", commands["seed_region"])
        self.assertIn("--region-code TR_PUBLIC_SOURCE", commands["layer_a_plan"])
        self.assertIn("--region-code TR_PUBLIC_SOURCE", commands["layer_a_run"])
        self.assertIn("--region-code TR_PUBLIC_SOURCE", commands["official_layer_a_import"])
        self.assertIn("--region-code TR_PUBLIC_SOURCE", commands["osm_forest_mask_run"])
        self.assertIn("--region-code TR_PUBLIC_SOURCE", commands["osm_bulk_masks_run"])
        self.assertIn("prepare_stage3_source_mask_cache", commands["source_mask_cache_prepare"])
        self.assertIn("--source-region-code TR_PUBLIC_SOURCE", commands["source_mask_cache_prepare"])
        self.assertIn("--source-layers ogm_esatis_bolme,osm_forest_mask,osm_nonforest_exclusion", commands["source_mask_cache_prepare"])
        self.assertNotIn("--min-forest-component-area-ha", commands["source_mask_cache_prepare"])
        self.assertIn("--source-region-code TR_PUBLIC_SOURCE", commands["forest_region_from_layer_a"])
        self.assertIn("--region-code TR_BOLU_TEST", commands["forest_region_from_layer_a"])
        self.assertIn("run_stage3_prepare_region_fragments", commands["forest_region_from_layer_a"])
        self.assertNotIn("--from-layer-a", commands["forest_region_from_layer_a"])
        self.assertNotIn("--clip-to-grid-version", commands["forest_region_from_layer_a"])
        self.assertIn("--source-layers ogm_esatis_bolme,osm_forest_mask", commands["forest_region_from_layer_a"])
        self.assertNotIn("--intersect-mask-source-layers", commands["forest_region_from_layer_a"])
        self.assertIn("--difference-mask-source-layers osm_nonforest_exclusion", commands["forest_region_from_layer_a"])
        self.assertNotIn("esa_worldcover_nonforest_exclusion", commands["forest_region_from_layer_a"])
        self.assertIn("--min-mask-component-area-ha 1.0", commands["forest_region_from_layer_a"])
        self.assertIn("--tile-size-m 10000", commands["forest_region_from_layer_a"])
        self.assertIn("--snap-grid-m 0 ", commands["forest_region_from_layer_a"])
        self.assertIn("--edge-tolerance-m 0.02", commands["forest_region_from_layer_a"])
        self.assertIn("--subdivide-max-vertices 256", commands["forest_region_from_layer_a"])
        self.assertNotIn("--use-source-mask-cache", commands["forest_region_from_layer_a"])
        self.assertIn("--min-mask-component-area-ha 1.0", commands["grid_run"])
        self.assertIn("--use-region-mask-components", commands["grid_run"])
        self.assertIn("--min-area-ha 26.65", commands["forest_region_from_layer_a"])
        self.assertIn("--max-area-ha 559.65", commands["forest_region_from_layer_a"])
        self.assertNotIn("--min-area-ha 20000000", commands["forest_region_from_layer_a"])
        self.assertIn("ingest_osm_forest_mask", commands["osm_forest_mask_run"])
        self.assertIn(
            "ingest_worldcover_nonforest_exclusion",
            commands["worldcover_nonforest_exclusion_run"],
        )
        self.assertIn("--bbox-from-region", commands["osm_forest_mask_run"])
        self.assertIn("ingest_osm_pbf_masks", commands["osm_bulk_masks_run"])
        self.assertIn("--mode all", commands["osm_bulk_masks_run"])
        self.assertIn("--mode forest", commands["osm_bulk_forest_run"])
        self.assertIn("--mode nonforest-polygons", commands["osm_bulk_nonforest_polygons_run"])
        self.assertIn("--mode nonforest-lines", commands["osm_bulk_nonforest_lines_run"])
        self.assertIn("turkey-latest.osm.pbf", commands["osm_bulk_masks_run"])
        self.assertIn("--force-download", commands["osm_bulk_masks_run"])
        self.assertIn("schema-owner", commands["osm_bulk_masks_run"])
        self.assertIn("run_stage3_official_layer_a_import", commands["official_layer_a_import"])
        self.assertIn("--file <official-layer-a.geojson>", commands["official_layer_a_import"])
        self.assertIn("--max-failed-tiles 0", commands["grid_run"])
        self.assertIn("--adopt-existing", commands["grid_adopt_existing"])
        self.assertIn("check_stage3_forest_clip", commands["forest_clip_check"])
        self.assertIn("--grid-version stage3-v1", commands["forest_clip_check"])
        self.assertIn("--region-code TR_BOLU_TEST", commands["readiness_report"])
        self.assertIn("--grid-version stage3-v1", commands["readiness_report"])
        self.assertIn("--acceptance-scope public-ogm", commands["readiness_report"])
        self.assertIn("--expected-grid-cells 533", commands["readiness_report"])
        self.assertNotIn("--expected-ogm-tiles", commands["readiness_report"])
        self.assertNotIn("--expected-grid-cells 23000000", commands["readiness_report"])
        self.assertNotIn("--trend-months 12", commands["readiness_report"])
        self.assertNotIn("--min-ndvi-trend-coverage 0.9", commands["readiness_report"])
        self.assertIn("--performance-report stage3-performance-report.json", commands["readiness_report"])
        self.assertNotIn("verification_run", commands)
        self.assertNotIn("ndvi_history_run", commands)
        self.assertNotIn("carbon_run", commands)
        self.assertNotIn("deferred_verification_run", commands)
        self.assertNotIn("deferred_ndvi_history_run", commands)
        self.assertLess(
            list(commands).index("performance_report"),
            list(commands).index("readiness_report"),
        )
        self.assertIn("--region-code TR_BOLU_TEST", commands["performance_report"])
        self.assertIn("--grid-version stage3-v1", commands["performance_report"])
        self.assertIn("--max-avg-ms 250", commands["performance_report"])
        self.assertIn("--max-p95-ms 500", commands["performance_report"])
        self.assertIn("--output stage3-performance-report.json", commands["performance_report"])
        self.assertIn("run_stage3_scale_hardening", commands["scale_hardening"])
        self.assertIn("--apply-indexes", commands["scale_hardening"])
        self.assertIn("--analyze", commands["scale_hardening"])
        self.assertIn("--acceptance-scope public-ogm", commands["scale_hardening"])
        self.assertIn("--require-partitions", commands["scale_hardening"])
        self.assertIn("--allow-shadow-partitions", commands["scale_hardening"])
        self.assertIn("run_stage3_partition_rollout", commands["partition_rollout_plan"])
        self.assertIn("--allow-online-shadow", commands["partition_rollout_plan"])
        self.assertIn("--apply-shadow", commands["partition_rollout_plan"])
        self.assertIn("--emit-sql", commands["partition_rollout_plan"])
        self.assertIn("--retire-missing-bolme", commands["layer_a_run"])
        self.assertIn("--retire-missing-mescere", commands["layer_a_run"])
        self.assertIn("--source-retries 4", commands["layer_a_run"])
        self.assertIn("--retry-delay 5", commands["layer_a_run"])
        self.assertIn("--subdivide-failed-tiles 2", commands["layer_a_run"])
        self.assertIn("run_stage3_contract_audit", commands["contract_audit"])
        self.assertIn("--region-code TR_BOLU_TEST", commands["contract_audit"])
        self.assertIn("--grid-version stage3-v1", commands["contract_audit"])
        self.assertIn("--acceptance-scope public-ogm", commands["contract_audit"])
        self.assertIn("--target-hectares 23000000", commands["contract_audit"])
        self.assertIn("--expected-grid-cells 533", commands["contract_audit"])
        self.assertIn("--expected-layer-a-features 12", commands["contract_audit"])
        self.assertIn("--expected-ogm-tiles", commands["contract_audit"])
        self.assertIn("--performance-report stage3-performance-report.json", commands["contract_audit"])
        self.assertIn("scripts/stage3_v9_fragment_equivalence_gate.py", commands["clean_mask_integrity"])
        self.assertIn("check_stage3_grid_mask_proof", commands["grid_mask_proof"])
        self.assertIn("--max-area-gap-ha 2.0", commands["grid_mask_proof"])
        self.assertIn("--source-layers ogm_esatis_bolme,osm_forest_mask", commands["clean_mask_integrity"])
        self.assertNotIn("--intersect-source-layers", commands["clean_mask_integrity"])
        self.assertIn("--max-exclusion-overlap-m2 1.0", commands["clean_mask_integrity"])
        self.assertNotIn("ogm_source_agreement", commands)
        self.assertNotIn("osm_source_agreement", commands)
        self.assertIn("check_stage3_grid_spatial_integrity", commands["grid_spatial_integrity"])

    def test_public_scope_uses_same_equal_area_projection_after_forest_refine(self):
        class Args:
            acceptance_scope = "public-ogm"
            target_hectares = 23_000_000
            full_scale_projected_srid = 6933

        projected_srid = _plan_projected_srid(
            Args(),
            {
                "forest_only": True,
                "centroid_lon": 31.0,
                "centroid_lat": 40.0,
            },
        )

        self.assertEqual(projected_srid, 6933)

    def test_small_public_scope_keeps_full_scale_projection_before_mask_refine(self):
        class Args:
            acceptance_scope = "public-ogm"
            target_hectares = 3000
            full_scale_projected_srid = 6933

        self.assertEqual(_plan_projected_srid(Args(), {
            "forest_only": False, "centroid_lon": 31.0, "centroid_lat": 40.0,
        }), 6933)

    def test_full_scale_scope_requires_partition_hardening(self):
        class Args:
            region_code = "TR"
            grid_version = "stage3-full-v1"
            grid_size_m = 100
            grid_tile_size_m = 10000
            ogm_tile_size_deg = 0.12
            target_hectares = 23_000_000
            expected_layer_a_features = 100
            acceptance_scope = "full-scale"

        commands = _commands(Args(), projected_srid=6933, region_grid_cells=23_000_000, region=None)

        self.assertIn("--turkey-bbox", commands["seed_region"])
        self.assertIn("--min-area-ha 1", commands["forest_region_from_layer_a"])
        self.assertIn("--source-layers ogm_esatis_bolme,osm_forest_mask", commands["forest_region_from_layer_a"])
        self.assertNotIn("--intersect-mask-source-layers", commands["forest_region_from_layer_a"])
        self.assertIn("--difference-mask-source-layers osm_nonforest_exclusion", commands["forest_region_from_layer_a"])
        self.assertNotIn("esa_worldcover_nonforest_exclusion", commands["forest_region_from_layer_a"])
        self.assertIn("--min-mask-component-area-ha 1.0", commands["forest_region_from_layer_a"])
        self.assertIn("--min-mask-component-area-ha 1.0", commands["grid_run"])
        self.assertIn("--use-region-mask-components", commands["grid_run"])
        self.assertNotIn("--skip-region-clip", commands["osm_bulk_masks_run"])
        self.assertIn(
            "--nonforest-filter-source-layers ogm_esatis_bolme,osm_forest_mask",
            commands["osm_bulk_masks_run"],
        )
        self.assertNotIn("--skip-region-clip", commands["osm_bulk_forest_run"])
        self.assertIn(
            "--nonforest-filter-source-layers ogm_esatis_bolme,osm_forest_mask",
            commands["osm_bulk_nonforest_polygons_run"],
        )
        self.assertIn(
            "--nonforest-filter-source-layers ogm_esatis_bolme,osm_forest_mask",
            commands["osm_bulk_nonforest_lines_run"],
        )
        self.assertNotIn("--skip-region-clip", commands["osm_bulk_nonforest_polygons_run"])
        self.assertNotIn("--skip-region-clip", commands["osm_bulk_nonforest_lines_run"])
        self.assertIn("--require-partitions", commands["scale_hardening"])
        self.assertIn("--acceptance-scope full-scale", commands["scale_hardening"])
        self.assertNotIn("--allow-shadow-partitions", commands["scale_hardening"])
        self.assertIn("run_stage3_partition_rollout", commands["partition_rollout_plan"])
        self.assertIn("--apply-shadow", commands["partition_rollout_plan"])
        self.assertIn("--expected-grid-cells 23000000", commands["readiness_report"])
        self.assertIn("--acceptance-scope full-scale", commands["contract_audit"])
        self.assertIn("--expected-grid-cells 23000000", commands["contract_audit"])
        self.assertIn("--expected-ogm-tiles 9971", commands["contract_audit"])

    def test_full_scale_runbook_can_emit_explicit_approval_flag(self):
        class Args:
            region_code = "TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10"
            grid_version = "stage3-public-ogm-osm-union-clean-v10"
            grid_size_m = 100
            grid_tile_size_m = 10000
            ogm_tile_size_deg = 0.12
            target_hectares = 23_000_000
            expected_layer_a_features = 100
            acceptance_scope = "full-scale"
            ack_full_turkey_user_approved = True

        commands = _commands(Args(), projected_srid=6933, region_grid_cells=23_000_000, region=None)

        self.assertIn("--ack-full-turkey-user-approved", commands["seed_region"])
        self.assertIn("--ack-full-turkey-user-approved", commands["forest_region_from_layer_a"])
        self.assertIn("--ack-full-turkey-user-approved", commands["grid_run"])
        self.assertIn("--ack-full-turkey-user-approved", commands["grid_resume"])
        self.assertIn("--ack-full-turkey-user-approved", commands["source_mask_cache_prepare"])

    def test_public_scope_requires_partition_hardening_when_grid_is_large(self):
        class Args:
            region_code = "TR_PUBLIC"
            grid_version = "stage3-public-v1"
            grid_size_m = 100
            grid_tile_size_m = 10000
            ogm_tile_size_deg = 0.12
            target_hectares = 23_000_000
            expected_layer_a_features = 0
            acceptance_scope = "public-ogm"

        commands = _commands(Args(), projected_srid=6933, region_grid_cells=23_000_000, region=None)

        self.assertIn("--require-partitions", commands["scale_hardening"])
        self.assertIn("--acceptance-scope public-ogm", commands["scale_hardening"])
        self.assertIn("--allow-shadow-partitions", commands["scale_hardening"])
        self.assertIn("--acceptance-scope public-ogm", commands["contract_audit"])
        self.assertIn("--expected-grid-cells 23000000", commands["contract_audit"])

    def test_readiness_notes_report_missing_scale_work(self):
        notes = readiness_notes(
            current={
                "grid_cells": 533,
                "layer_a_features": 3,
                "grid_runs": 0,
                "layer_a_runs": 0,
            },
            schema={
                "grid_generation_tracking": True,
                "layer_a_ingest_tracking": True,
                "core_spatial_indexes": True,
                "partitioning_detected": False,
            },
            estimated_grid_cells=23_000_000,
            expected_layer_a_features=10,
        )

        self.assertIn("full region grid has not been generated for the selected grid version", notes)
        self.assertIn("Layer A feature count is below the supplied target", notes)
        self.assertIn("large grid target requires a partition rollout before production-scale loading", notes)


if __name__ == "__main__":
    unittest.main()
