import unittest

from app.commands.run_stage3_contract_audit import (
    REQUIRED_GIST_INDEXES,
    REQUIRED_QUERY_INDEXES,
    _acceptance_grid_target,
    _performance_expected_table_counts,
    build_contract_checklist as build_contract_checklist_raw,
    estimate_contract_grid_cells,
)


def ready_indexes() -> dict[str, bool]:
    return {name: True for name in (*REQUIRED_GIST_INDEXES, *REQUIRED_QUERY_INDEXES)}


def ready_forest_clip() -> dict:
    return {"checks": {"ready": True}}


def build_contract_checklist(**kwargs):
    kwargs.setdefault("forest_clip_report", ready_forest_clip())
    return build_contract_checklist_raw(**kwargs)


class Stage3ContractAuditTest(unittest.TestCase):
    def test_estimates_contract_grid_cells(self):
        self.assertEqual(estimate_contract_grid_cells(23_000_000, 100), 23_000_000)
        self.assertEqual(estimate_contract_grid_cells(10.1, 100), 11)
        self.assertEqual(estimate_contract_grid_cells(4, 200), 1)

    def test_public_acceptance_uses_supplied_forest_only_target(self):
        self.assertEqual(
            _acceptance_grid_target(
                acceptance_scope="public-ogm",
                expected_grid_cells=443,
                contract_target_grid_cells=23_000_000,
            ),
            443,
        )

    def test_full_scale_acceptance_keeps_contract_target(self):
        self.assertEqual(
            _acceptance_grid_target(
                acceptance_scope="full-scale",
                expected_grid_cells=443,
                contract_target_grid_cells=23_000_000,
            ),
            23_000_000,
        )

    def test_contract_audit_blocks_missing_full_scale_items(self):
        checklist = build_contract_checklist(
            state={
                "grid_cells": 2,
                "layer_a_features": 8,
                "osm_forest_features": 3,
                "osm_nonforest_features": 2,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 10,
                "public_layer_a_processed_tiles": 10,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
            },
            performance_report={
                "ready": False,
                "probe_names": [],
            },
            target_grid_cells=23_000_000,
            expected_layer_a_features=100,
            acceptance_scope="full-scale",
        )

        self.assertFalse(checklist["ready"])
        self.assertTrue(checklist["items"]["isolated_ogm_parser"])
        self.assertTrue(checklist["items"]["forest_only_grid_generation"])
        self.assertFalse(checklist["items"]["layer_a_full_load"])
        self.assertFalse(checklist["items"]["full_grid_load"])
        self.assertFalse(checklist["items"]["region_partitioning"])
        self.assertIn("full_grid_load", checklist["progress"]["pending_items"])
        self.assertIn("region_partitioning", checklist["progress"]["pending_items"])
        self.assertIn("full 1 ha grid is incomplete: 2 of 23000000", checklist["blockers"])

    def test_contract_audit_passes_when_every_contract_item_has_evidence(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 23_000_000,
                "layer_a_features": 100,
                "osm_forest_features": 20,
                "osm_nonforest_features": 10,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 50,
                "public_layer_a_processed_tiles": 50,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={
                "ready": True,
                "probe_names": probe_names,
            },
            target_grid_cells=23_000_000,
            expected_layer_a_features=100,
            acceptance_scope="full-scale",
        )

        self.assertTrue(checklist["ready"])
        self.assertEqual(checklist["blockers"], [])
        self.assertEqual(checklist["progress"]["pending_items"], [])

    def test_public_pilot_audit_does_not_close_contract_acceptance(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 533,
                "layer_a_features": 100,
                "osm_forest_features": 20,
                "osm_nonforest_features": 10,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 50,
                "public_layer_a_processed_tiles": 50,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={
                "ready": True,
                "probe_names": probe_names,
            },
            target_grid_cells=533,
            expected_layer_a_features=100,
            acceptance_scope="public-ogm",
        )

        self.assertFalse(checklist["ready"])
        self.assertTrue(checklist["items"]["region_partitioning"])
        self.assertIn(
            "public OGM/OSM scope is pilot-sized; full public forest-only rollout evidence is still required",
            checklist["blockers"],
        )
        self.assertEqual(checklist["progress"]["acceptance_mode"], "public-pilot")

    def test_public_scope_uses_supplied_public_target_not_official_contract_reference(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 1_000_000,
                "layer_a_features": 100,
                "osm_forest_features": 20,
                "osm_nonforest_features": 10,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 50,
                "public_layer_a_processed_tiles": 50,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={
                "ready": True,
                "probe_names": probe_names,
            },
            target_grid_cells=1_000_000,
            contract_target_grid_cells=23_000_000,
            expected_layer_a_features=100,
            acceptance_scope="public-ogm",
        )

        self.assertTrue(checklist["ready"])
        self.assertTrue(checklist["items"]["full_grid_load"])
        self.assertTrue(checklist["items"]["region_partitioning"])
        self.assertEqual(checklist["blockers"], [])
        self.assertEqual(checklist["progress"]["contract_grid_reference"]["target"], 1_000_000)

    def test_full_scale_requires_active_region_partitions(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 23_000_000,
                "layer_a_features": 100,
                "osm_forest_features": 20,
                "osm_nonforest_features": 10,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 50,
                "public_layer_a_processed_tiles": 50,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={
                "ready": True,
                "probe_names": probe_names,
            },
            target_grid_cells=23_000_000,
            expected_layer_a_features=100,
            acceptance_scope="full-scale",
        )

        self.assertFalse(checklist["ready"])
        self.assertFalse(checklist["items"]["region_partitioning"])
        self.assertIn(
            "missing region partitioning: grid_cells, sentinel_observations, verification_results",
            checklist["blockers"],
        )

    def test_public_scope_uses_public_ogm_osm_evidence_instead_of_official_target(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 533,
                "layer_a_features": 12,
                "osm_forest_features": 8,
                "osm_nonforest_features": 5,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 4,
                "public_layer_a_processed_tiles": 4,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=533,
            expected_layer_a_features=0,
            acceptance_scope="public-ogm",
        )

        self.assertFalse(checklist["ready"])
        self.assertTrue(checklist["items"]["layer_a_full_load"])
        self.assertEqual(checklist["progress"]["layer_a_features"]["target"], None)
        self.assertIn(
            "public OGM/OSM scope is pilot-sized; full public forest-only rollout evidence is still required",
            checklist["blockers"],
        )

    def test_public_scope_blocks_when_public_layer_a_run_is_not_complete(self):
        checklist = build_contract_checklist(
            state={
                "grid_cells": 533,
                "layer_a_features": 12,
                "osm_forest_features": 0,
                "osm_nonforest_features": 0,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": False,
                "public_layer_a_total_tiles": 4,
                "public_layer_a_processed_tiles": 3,
                "osm_forest_completed_check": False,
                "osm_nonforest_completed_check": False,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": []},
            target_grid_cells=533,
            expected_layer_a_features=0,
            acceptance_scope="public-ogm",
        )

        self.assertFalse(checklist["ready"])
        self.assertIn("public OGM/OSM source checks are incomplete", checklist["blockers"])
        self.assertIn("public OSM forest mask has no current features", checklist["blockers"])

    def test_public_scope_requires_completed_ogm_layer_a_run(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 8_824_027,
                "layer_a_features": 19_058,
                "osm_forest_features": 61_351,
                "osm_nonforest_features": 261_219,
                "osm_nonforest_polygon_features": 48_244,
                "osm_nonforest_line_features": 212_975,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": False,
                "public_layer_a_total_tiles": 4_410,
                "public_layer_a_processed_tiles": 4_410,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
                "osm_nonforest_polygon_completed_check": True,
                "osm_nonforest_line_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=8_824_027,
            contract_target_grid_cells=23_000_000,
            expected_layer_a_features=19_058,
            acceptance_scope="public-ogm",
        )

        self.assertFalse(checklist["items"]["layer_a_full_load"])
        self.assertEqual(checklist["progress"]["layer_a_features"]["current"], 19_058)
        self.assertIn("public OGM/OSM source checks are incomplete", checklist["blockers"])

    def test_public_performance_expected_counts_use_full_public_source_features(self):
        counts = _performance_expected_table_counts(
            {
                "grid_cells": 8_824_027,
                "layer_a_features": 19_058,
                "osm_forest_features": 61_351,
                "ndvi_observations": 25,
                "verification_results": 25,
                "carbon_assessments": 25,
                "carbon_audit_events": 25,
            },
            "public-ogm",
        )

        self.assertEqual(counts["layer_a_features"], 19_058)

    def test_public_scope_blocks_when_ogm_tile_coverage_is_too_small(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 533,
                "layer_a_features": 12,
                "osm_forest_features": 8,
                "osm_nonforest_features": 5,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 2,
                "public_layer_a_processed_tiles": 2,
                "osm_forest_completed_check": False,
                "osm_nonforest_completed_check": False,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=533,
            expected_layer_a_features=0,
            expected_ogm_tiles=4,
            acceptance_scope="public-ogm",
        )

        self.assertFalse(checklist["ready"])
        self.assertIn("public OGM Layer A tile coverage is incomplete: 2 of 4", checklist["blockers"])

    def test_full_scale_blocks_when_only_osm_bulk_source_is_available(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 23_000_000,
                "layer_a_features": 0,
                "osm_forest_features": 120_000,
                "osm_nonforest_features": 60_000,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": False,
                "public_layer_a_total_tiles": 0,
                "public_layer_a_processed_tiles": 0,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=23_000_000,
            expected_layer_a_features=0,
            expected_ogm_tiles=9971,
            acceptance_scope="full-scale",
        )

        self.assertFalse(checklist["ready"])
        self.assertFalse(checklist["items"]["layer_a_full_load"])
        self.assertIn("public OGM/OSM Layer A source has no completed full-scale run", checklist["blockers"])
        self.assertIn("public OGM Layer A full-scale tile coverage is incomplete: 0 of 9971", checklist["blockers"])
        self.assertIn("public OGM/OSM Layer A full-scale load has no current features", checklist["blockers"])

    def test_full_scale_blocks_without_nonforest_difference_mask(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 23_000_000,
                "layer_a_features": 0,
                "osm_forest_features": 120_000,
                "osm_nonforest_features": 0,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": False,
                "public_layer_a_total_tiles": 0,
                "public_layer_a_processed_tiles": 0,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": False,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=23_000_000,
            expected_layer_a_features=0,
            expected_ogm_tiles=9971,
            acceptance_scope="full-scale",
        )

        self.assertFalse(checklist["ready"])
        self.assertFalse(checklist["items"]["layer_a_full_load"])
        self.assertIn(
            "public OSM non-forest Difference mask has no completed full-scale polygon and line run",
            checklist["blockers"],
        )

    def test_full_scale_blocks_when_only_one_nonforest_component_is_loaded(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 23_000_000,
                "layer_a_features": 0,
                "osm_forest_features": 120_000,
                "osm_nonforest_features": 60_000,
                "osm_nonforest_polygon_features": 60_000,
                "osm_nonforest_line_features": 0,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": False,
                "public_layer_a_total_tiles": 0,
                "public_layer_a_processed_tiles": 0,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
                "osm_nonforest_polygon_completed_check": True,
                "osm_nonforest_line_completed_check": False,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=23_000_000,
            expected_layer_a_features=0,
            expected_ogm_tiles=9971,
            acceptance_scope="full-scale",
        )

        self.assertFalse(checklist["ready"])
        self.assertIn(
            "public OSM non-forest Difference mask has no completed full-scale polygon and line run",
            checklist["blockers"],
        )

    def test_accepts_completed_empty_nonforest_difference_components(self):
        probe_names = [
            "asset_register_page",
            "asset_register_count",
            "raw_grid_mvt_tile",
            "layer_b_mvt_tile",
            "layer_c_mvt_tile",
            "layer_a_intersections",
            "latest_ndvi_history",
            "carbon_asset_page",
            "carbon_audit_tail",
        ]
        checklist = build_contract_checklist(
            state={
                "grid_cells": 49,
                "layer_a_features": 8,
                "osm_forest_features": 2,
                "osm_nonforest_features": 0,
                "osm_nonforest_polygon_features": 0,
                "osm_nonforest_line_features": 0,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 1,
                "public_layer_a_processed_tiles": 1,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
                "osm_nonforest_polygon_completed_check": True,
                "osm_nonforest_line_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": probe_names},
            target_grid_cells=49,
            contract_target_grid_cells=23_000_000,
            expected_layer_a_features=8,
            expected_ogm_tiles=1,
            acceptance_scope="public-ogm",
        )

        self.assertFalse(checklist["ready"])
        self.assertTrue(checklist["items"]["layer_a_full_load"])
        self.assertTrue(checklist["items"]["region_partitioning"])
        self.assertIn(
            "public OGM/OSM scope is pilot-sized; full public forest-only rollout evidence is still required",
            checklist["blockers"],
        )

    def test_contract_audit_requires_forest_clip_report(self):
        checklist = build_contract_checklist_raw(
            state={
                "grid_cells": 23_000_000,
                "layer_a_features": 100,
                "osm_forest_features": 20,
                "osm_nonforest_features": 10,
                "forest_only_region": True,
                "forest_only_grid_run": True,
                "public_layer_a_completed_run": True,
                "public_layer_a_total_tiles": 50,
                "public_layer_a_processed_tiles": 50,
                "osm_forest_completed_check": True,
                "osm_nonforest_completed_check": True,
            },
            schema={
                "layer_a_ingest_tracking": True,
                "source_adapter_boundary": True,
                "indexes": ready_indexes(),
                "region_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            performance_report={"ready": True, "probe_names": [
                "asset_register_page",
                "asset_register_count",
                "raw_grid_mvt_tile",
                "layer_b_mvt_tile",
                "layer_c_mvt_tile",
                "layer_a_intersections",
                "latest_ndvi_history",
                "carbon_asset_page",
                "carbon_audit_tail",
            ]},
            target_grid_cells=23_000_000,
            expected_layer_a_features=100,
            acceptance_scope="full-scale",
        )

        self.assertFalse(checklist["ready"])
        self.assertFalse(checklist["items"]["forest_only_grid_generation"])
        self.assertIn("forest clip report is missing", checklist["blockers"])


if __name__ == "__main__":
    unittest.main()
