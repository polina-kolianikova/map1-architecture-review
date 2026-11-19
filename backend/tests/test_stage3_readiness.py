import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from app.commands.run_stage3_readiness import (
    REQUIRED_PERFORMANCE_PROBES,
    _contract_checklist,
    _future_layer_a_adapter_boundary_ready,
    _grid_run_sql,
    _has_completed_grid_run,
    _has_forest_only_grid_run,
    _has_completed_layer_a_run,
    _load_performance_report,
    _load_ready_report,
    _query_probe_sql,
    _readiness,
    _schema_status,
    _layer_a_run_sql,
    _source_sql,
    _summary_sql,
    _source_health,
    _stage3_acceptance_summary,
)


def completed_layer_a_run(**overrides):
    run = {
        "source": "all",
        "status": "completed",
        "total_tiles": 1,
        "processed_tiles": 1,
        "bolme_inserted": 7,
        "mescere_inserted": 3,
        "failed_bolme": 0,
        "failed_mescere": 0,
        "max_tiles": 0,
        "bbox_source": "region",
        "run_region_geometry_hash": "current",
        "current_region_geometry_hash": "current",
    }
    run.update(overrides)
    return run


class Stage3ReadinessTest(unittest.TestCase):
    def test_readiness_layer_a_reads_use_source_region_scope(self):
        summary_sql = str(_summary_sql())
        source_sql = str(_source_sql())
        run_sql = str(_layer_a_run_sql())

        self.assertIn("WITH source_scope AS", summary_sql)
        self.assertIn("AS layer_a_region_code", summary_sql)
        self.assertIn("SELECT layer_a_region_code", summary_sql)
        self.assertIn("region_scope.mask_source_region_code AS source_region_code", source_sql)
        self.assertIn("WITH region_scope AS", run_sql)
        self.assertIn("region.code = region_scope.layer_a_region_code", run_sql)

    def test_readiness_grid_run_uses_component_hash_for_component_mask_runs(self):
        grid_sql = str(_grid_run_sql())

        self.assertIn("stage3_region_mask_components", grid_sql)
        self.assertIn("grid_mask_method' = 'region_mask_components", grid_sql)
        self.assertIn("component_hashes.source_hash", grid_sql)

    def test_query_probe_uses_scoped_verification_aggregate_without_sorting(self):
        probe_sql = str(_query_probe_sql())

        self.assertIn("FROM verification_results result", probe_sql)
        self.assertIn("result.region_id", probe_sql)
        self.assertNotIn("DISTINCT ON", probe_sql)
        self.assertNotIn("ORDER BY result.grid_cell_id", probe_sql)

    def test_schema_status_reports_missing_objects(self):
        status = _schema_status(
            table_rows=[
                {"name": "grid_cells", "present": True},
                {"name": "carbon_assessments", "present": False},
            ],
            index_rows=[
                {"name": "grid_cells_geom_gix", "present": True},
                {"name": "carbon_assessments_current_status_cell_idx", "present": False},
            ],
        )

        self.assertFalse(status["tables_ready"])
        self.assertFalse(status["indexes_ready"])
        self.assertEqual(status["missing_tables"], ["carbon_assessments"])
        self.assertEqual(status["missing_indexes"], ["carbon_assessments_current_status_cell_idx"])

    def test_schema_status_reports_missing_partitioning(self):
        status = _schema_status(
            table_rows=[{"name": "grid_cells", "present": True}],
            index_rows=[{"name": "grid_cells_geom_gix", "present": True}],
            partition_rows=[
                {"name": "grid_cells", "partitioned": False},
                {"name": "verification_results", "partitioned": True},
            ],
        )

        self.assertFalse(status["partitioning_ready"])
        self.assertEqual(status["missing_partitions"], ["grid_cells", "verification_results"])

    def test_schema_status_reports_validated_shadow_partitioning(self):
        status = _schema_status(
            table_rows=[{"name": "grid_cells", "present": True}],
            index_rows=[{"name": "grid_cells_geom_gix", "present": True}],
            partition_rows=[
                {"name": "grid_cells", "partitioned": False, "partition_key": ""},
            ],
            shadow_partition_rows=[
                {"name": "grid_cells", "partitioned": True, "partition_key": "LIST (region_id)"},
            ],
            shadow_partition_counts_match={"grid_cells": True},
        )

        self.assertFalse(status["active_partitioning_ready"])
        self.assertTrue(status["shadow_partitioning_ready"])
        self.assertFalse(status["partitioning_ready"])

    def test_readiness_requires_schema_for_full_scale(self):
        summary = {
            "grid_cells": 23000000,
            "layer_a_features": 100,
            "verification_results": 23000000,
            "ndvi_observations": 23000000,
            "carbon_assessments": 23000000,
            "calculated_carbon_assessments": 23000000,
            "carbon_audit_events": 23000000,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": False,
            "partitioning_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"status": "ok"}],
            schema=schema,
            expected_grid_cells=23000000,
            expected_layer_a_features=1,
        )

        self.assertTrue(readiness["bounded_workspace_ready"])
        self.assertFalse(readiness["stage3_schema_ready"])
        self.assertFalse(readiness["full_scale_ready"])

    def test_readiness_requires_partitioning_for_large_grid_target(self):
        summary = {
            "grid_cells": 23000000,
            "layer_a_features": 100,
            "verification_results": 23000000,
            "ndvi_observations": 23000000,
            "carbon_assessments": 23000000,
            "calculated_carbon_assessments": 23000000,
            "carbon_audit_events": 23000000,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": False,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"status": "ok"}],
            schema=schema,
            expected_grid_cells=23000000,
            expected_layer_a_features=1,
        )

        self.assertTrue(readiness["full_scale_partitioning_required"])
        self.assertFalse(readiness["full_scale_ready"])

    def test_public_readiness_accepts_validated_shadow_partitioning_for_large_target(self):
        summary = {
            "grid_cells": 1000000,
            "layer_a_features": 100,
            "verification_results": 1000000,
            "ndvi_observations": 1000000,
            "ndvi_trend_cells": 1000000,
            "carbon_assessments": 1000000,
            "calculated_carbon_assessments": 1000000,
            "skipped_missing_evidence_carbon_assessments": 0,
            "baseline_calculated_carbon_assessments": 1000000,
            "historical_trend_carbon_assessments": 1000000,
            "forest_area_carbon_assessments": 1000000,
            "carbon_audit_events": 1000000,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": False,
            "active_partitioning_ready": False,
            "shadow_partitioning_ready": True,
            "missing_partitions": ["grid_cells", "sentinel_observations", "verification_results"],
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"source_layer": "ogm_esatis_bolme", "status": "ok"}],
            schema=schema,
            expected_grid_cells=1000000,
            expected_layer_a_features=100,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 1000000,
                }
            ],
            layer_a_runs=[completed_layer_a_run()],
            expected_source_layers=["ogm_esatis_bolme"],
            performance_report_ready=True,
            acceptance_scope="public-ogm",
        )

        self.assertTrue(readiness["full_scale_partitioning_required"])
        self.assertFalse(readiness["active_partitioning_ready"])
        self.assertTrue(readiness["shadow_partitioning_ready"])
        self.assertTrue(readiness["partitioning_ready"])

    def test_full_scale_readiness_does_not_accept_shadow_partitioning_as_cutover(self):
        summary = {
            "grid_cells": 23000000,
            "layer_a_features": 100,
            "verification_results": 23000000,
            "ndvi_observations": 23000000,
            "ndvi_trend_cells": 23000000,
            "carbon_assessments": 23000000,
            "calculated_carbon_assessments": 23000000,
            "historical_trend_carbon_assessments": 23000000,
            "forest_area_carbon_assessments": 23000000,
            "approved_calculated_carbon_assessments": 23000000,
            "carbon_audit_events": 23000000,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": False,
            "active_partitioning_ready": False,
            "shadow_partitioning_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"source_layer": "ogm_esatis_bolme", "status": "ok"}],
            schema=schema,
            expected_grid_cells=23000000,
            expected_layer_a_features=100,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 23000000,
                }
            ],
            layer_a_runs=[completed_layer_a_run()],
            expected_source_layers=["ogm_esatis_bolme"],
            performance_report_ready=True,
            acceptance_scope="full-scale",
        )

        self.assertFalse(readiness["partitioning_ready"])
        self.assertFalse(readiness["full_scale_ready"])

    def test_readiness_requires_all_expected_layer_a_sources_ok(self):
        summary = {
            "grid_cells": 10,
            "layer_a_features": 10,
            "verification_results": 10,
            "ndvi_observations": 10,
            "carbon_assessments": 10,
            "calculated_carbon_assessments": 10,
            "carbon_audit_events": 10,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[
                {"source_layer": "ogm_esatis_bolme", "status": "ok"},
                {"source_layer": "ogm_yayin_mescere_wms_svg", "status": "unavailable"},
            ],
            schema=schema,
            expected_grid_cells=10,
            expected_layer_a_features=10,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 10,
                }
            ],
            layer_a_runs=[],
            expected_source_layers=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg"],
            performance_report_ready=True,
        )

        self.assertEqual(readiness["source_health"], "retained_or_pending")
        self.assertFalse(readiness["full_scale_ready"])

    def test_readiness_requires_ndvi_trend_coverage_when_threshold_is_set(self):
        summary = {
            "grid_cells": 10,
            "layer_a_features": 10,
            "verification_results": 10,
            "ndvi_observations": 10,
            "ndvi_trend_cells": 6,
            "carbon_assessments": 10,
            "calculated_carbon_assessments": 10,
            "carbon_audit_events": 10,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"source_layer": "ogm_esatis_bolme", "status": "ok"}],
            schema=schema,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 10,
                }
            ],
            layer_a_runs=[completed_layer_a_run()],
            expected_source_layers=["ogm_esatis_bolme"],
            performance_report_ready=True,
            min_ndvi_trend_coverage=0.9,
        )
        report = {
            "scope": {"acceptance_scope": "public-ogm"},
            "summary": summary,
            "schema": {
                "tables_ready": True,
                "indexes_ready": True,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_partitions": [],
            },
            "operational": {"ogm_tls_verify": True},
            "readiness": readiness,
        }

        checklist = _contract_checklist(report)

        self.assertFalse(readiness["ndvi_trend_ready"])
        self.assertFalse(checklist["ready_for_contract_acceptance"])
        self.assertFalse(readiness["public_ogm_pipeline_ready"])
        self.assertNotIn(
            "NDVI trend history does not meet the selected coverage threshold: 6 of 10",
            checklist["blockers"],
        )

    def test_public_readiness_accepts_audited_missing_carbon_evidence(self):
        now = datetime.now(UTC).isoformat()
        summary = {
            "region_metadata": {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {
                    "source": "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
                    "source_layers": ["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg"],
                    "intersect_source_layers": ["osm_forest_mask"],
                    "difference_source_layers": [
                        "osm_nonforest_exclusion",
                        "esa_worldcover_nonforest_exclusion",
                    ],
                    "source_qualities": ["public_ogm_rest", "public_wms_derived"],
                    "intersect_source_qualities": ["public_osm_forest"],
                    "difference_source_qualities": [
                        "public_osm_nonforest_exclusion",
                        "public_satellite_landcover_nonforest_exclusion",
                    ],
                    "production_roles": ["active_public_layer_a", "active_public_layer_a"],
                    "intersect_production_roles": ["auxiliary_public_forest_mask"],
                    "difference_production_roles": [
                        "auxiliary_public_nonforest_exclusion",
                        "auxiliary_public_nonforest_exclusion",
                    ],
                },
                "future_full_layer_a_source_adapter": True,
            },
            "forest_only_region": True,
            "region_source": "forest_mask_layer_a",
            "grid_cells": 31,
            "layer_a_features": 8,
            "verification_results": 31,
            "verification_unexpected_layer_a_sources": 0,
            "verification_source_layer_mismatches": 0,
            "ndvi_observations": 30,
            "ndvi_trend_cells": 30,
            "carbon_assessments": 31,
            "calculated_carbon_assessments": 30,
            "skipped_missing_evidence_carbon_assessments": 1,
            "baseline_calculated_carbon_assessments": 30,
            "historical_trend_carbon_assessments": 30,
            "forest_area_carbon_assessments": 30,
            "carbon_audit_events": 31,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": False,
            "region_metadata_column_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[
                {"source_layer": "ogm_esatis_bolme", "status": "ok", "checked_at": now},
                {"source_layer": "ogm_yayin_mescere_wms_svg", "status": "ok", "checked_at": now},
                {"source_layer": "osm_forest_mask", "status": "ok", "checked_at": now},
            ],
            schema=schema,
            expected_grid_cells=31,
            expected_layer_a_features=8,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 31,
                    "forest_only_run": True,
                    "run_region_geometry_hash": "current",
                    "current_region_geometry_hash": "current",
                }
            ],
            layer_a_runs=[completed_layer_a_run()],
            expected_source_layers=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg", "osm_forest_mask"],
            performance_report_ready=True,
            min_ndvi_trend_coverage=0.9,
            acceptance_scope="public-ogm",
            forest_clip={"checks": {"ready": True}},
        )

        self.assertTrue(readiness["public_ogm_pipeline_ready"])
        self.assertFalse(readiness["calculated_carbon_complete"])
        self.assertEqual(readiness["carbon_evidence_coverage"], 1.0)
        self.assertEqual(readiness["calculated_carbon_coverage"], 0.96774)

    def test_public_readiness_accepts_filtered_component_mask_source(self):
        now = datetime.now(UTC).isoformat()
        summary = {
            "region_metadata": {
                "stage3_region_source": "filtered_final_forest_mask_components",
                "forest_only": True,
                "forest_mask": {
                    "source": "public_osm_forest_mask_with_nonforest_difference",
                    "mask_version": "TR-STAGE3-PUBLIC-OSM-WC-FOREST-V4",
                    "mask_components_table": "stage3_region_mask_components",
                    "source_layers": ["osm_forest_mask"],
                    "difference_source_layers": [
                        "osm_nonforest_exclusion",
                        "esa_worldcover_nonforest_exclusion",
                    ],
                    "source_qualities": ["public_osm_forest"],
                    "difference_source_qualities": [
                        "public_osm_nonforest_exclusion",
                        "public_satellite_landcover_nonforest_exclusion",
                    ],
                    "production_roles": ["auxiliary_public_forest_mask"],
                    "difference_production_roles": [
                        "auxiliary_public_nonforest_exclusion",
                        "auxiliary_public_nonforest_exclusion",
                    ],
                    "clean_mask_policy": {
                        "method": "component_area_filter_before_grid_generation",
                        "min_component_area_ha": 1.0,
                        "applies_before_grid_insert": True,
                    },
                },
                "future_full_layer_a_source_adapter": True,
            },
            "forest_only_region": True,
            "region_source": "filtered_final_forest_mask_components",
            "grid_cells": 100,
            "layer_a_features": 100,
            "verification_results": 100,
            "verification_unexpected_layer_a_sources": 0,
            "verification_source_layer_mismatches": 0,
            "ndvi_observations": 20,
            "ndvi_trend_cells": 0,
            "carbon_assessments": 100,
            "calculated_carbon_assessments": 20,
            "skipped_missing_evidence_carbon_assessments": 80,
            "baseline_calculated_carbon_assessments": 20,
            "historical_trend_carbon_assessments": 0,
            "forest_area_carbon_assessments": 20,
            "carbon_audit_events": 100,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": False,
            "shadow_partitioning_ready": True,
            "region_metadata_column_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[
                {"source_layer": "osm_forest_mask", "status": "ok", "checked_at": now},
                {"source_layer": "osm_nonforest_exclusion", "status": "ok", "checked_at": now},
                {"source_layer": "esa_worldcover_nonforest_exclusion", "status": "ok", "checked_at": now},
            ],
            schema=schema,
            expected_grid_cells=100,
            expected_layer_a_features=100,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 10,
                    "processed_tiles": 10,
                    "inserted_cells": 100,
                    "forest_only_run": True,
                    "grid_mask_method": "region_mask_components",
                    "mask_version": "TR-STAGE3-PUBLIC-OSM-WC-FOREST-V4",
                    "run_region_geometry_hash": "component",
                    "current_region_geometry_hash": "component",
                }
            ],
            layer_a_runs=[],
            expected_source_layers=["osm_forest_mask"],
            performance_report_ready=True,
            min_ndvi_trend_coverage=0.9,
            acceptance_scope="public-ogm",
            forest_clip={"checks": {"ready": True}},
        )

        self.assertTrue(readiness["stage3_grid_run_ready"])
        self.assertTrue(readiness["forest_only_grid_run_ready"])
        self.assertTrue(readiness["forest_mask_source_ready"])
        self.assertTrue(readiness["carbon_calculation_ready"])
        self.assertFalse(readiness["ndvi_trend_ready"])
        self.assertFalse(readiness["public_ogm_pipeline_ready"])

    def test_readiness_requires_audit_event_for_each_current_carbon_assessment(self):
        summary = {
            "grid_cells": 10,
            "layer_a_features": 10,
            "verification_results": 10,
            "ndvi_observations": 10,
            "ndvi_trend_cells": 10,
            "carbon_assessments": 10,
            "calculated_carbon_assessments": 10,
            "carbon_audit_events": 99,
            "carbon_assessments_without_audit": 1,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"source_layer": "ogm_esatis_bolme", "status": "ok"}],
            schema=schema,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 10,
                }
            ],
            layer_a_runs=[completed_layer_a_run()],
            expected_source_layers=["ogm_esatis_bolme"],
            performance_report_ready=True,
            min_ndvi_trend_coverage=0.9,
        )

        self.assertFalse(readiness["carbon_audit_ready"])
        self.assertFalse(readiness["public_ogm_pipeline_ready"])

    def test_completed_layer_a_run_does_not_hide_unhealthy_sources(self):
        readiness = _readiness(
            {
                "grid_cells": 10,
                "layer_a_features": 10,
                "verification_results": 10,
                "ndvi_observations": 10,
                "carbon_assessments": 10,
                "calculated_carbon_assessments": 10,
                "carbon_audit_events": 10,
                "carbon_assessments_without_audit": 0,
            },
            sources=[
                {"source_layer": "ogm_esatis_bolme", "status": "empty"},
                {"source_layer": "ogm_yayin_mescere_wms_svg", "status": "empty"},
            ],
            schema={
                "tables_ready": True,
                "indexes_ready": True,
                "partitioning_ready": True,
                "tables": {
                    "stage3_grid_generation_runs": True,
                    "stage3_grid_generation_tiles": True,
                    "stage3_layer_a_ingest_runs": True,
                    "stage3_layer_a_ingest_tiles": True,
                },
            },
            expected_grid_cells=10,
            expected_layer_a_features=10,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 10,
                }
            ],
            layer_a_runs=[completed_layer_a_run(total_tiles=3, processed_tiles=3, bolme_inserted=5, mescere_inserted=5)],
            expected_source_layers=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg"],
            performance_report_ready=True,
        )

        self.assertEqual(readiness["source_health"], "retained_or_pending")

    def test_readiness_rejects_stale_public_source_checks(self):
        readiness = _readiness(
            {
                "grid_cells": 10,
                "layer_a_features": 10,
                "verification_results": 10,
                "ndvi_observations": 10,
                "carbon_assessments": 10,
                "calculated_carbon_assessments": 10,
                "carbon_audit_events": 10,
                "carbon_assessments_without_audit": 0,
            },
            sources=[
                {
                    "source_layer": "ogm_esatis_bolme",
                    "status": "ok",
                    "checked_at": (datetime.now(UTC) - timedelta(days=9)).isoformat(),
                },
            ],
            schema={
                "tables_ready": True,
                "indexes_ready": True,
                "partitioning_ready": True,
                "tables": {
                    "stage3_grid_generation_runs": True,
                    "stage3_grid_generation_tiles": True,
                    "stage3_layer_a_ingest_runs": True,
                    "stage3_layer_a_ingest_tiles": True,
                },
            },
            expected_source_layers=["ogm_esatis_bolme"],
            performance_report_ready=True,
            max_source_age_days=7,
        )

        self.assertEqual(readiness["source_health"], "stale")
        self.assertFalse(readiness["public_ogm_pipeline_ready"])

    def test_source_health_requires_auxiliary_osm_source_check(self):
        sources = [
            {"source_layer": "ogm_esatis_bolme", "status": "ok"},
            {"source_layer": "ogm_yayin_mescere_wms_svg", "status": "ok"},
        ]

        health = _source_health(
            sources,
            layer_a_runs=[],
            expected_sources=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg", "osm_forest_mask"],
        )

        self.assertEqual(health, "retained_or_pending")

    def test_source_health_accepts_fresh_osm_auxiliary_mask(self):
        now = datetime.now(UTC).isoformat()
        sources = [
            {"source_layer": "ogm_esatis_bolme", "status": "ok", "checked_at": now},
            {"source_layer": "ogm_yayin_mescere_wms_svg", "status": "ok", "checked_at": now},
            {"source_layer": "osm_forest_mask", "status": "ok", "checked_at": now},
        ]

        health = _source_health(
            sources,
            layer_a_runs=[],
            expected_sources=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg", "osm_forest_mask"],
        )

        self.assertEqual(health, "ok")

    def test_completed_layer_a_run_rejects_limited_rehearsal(self):
        self.assertFalse(
            _has_completed_layer_a_run(
                [
                    {
                        "source": "all",
                        "status": "completed",
                        "total_tiles": 1,
                        "processed_tiles": 1,
                        "bolme_inserted": 5,
                        "mescere_inserted": 5,
                        "failed_bolme": 0,
                        "failed_mescere": 0,
                        "max_tiles": 1,
                    }
                ]
            )
        )

    def test_completed_layer_a_run_accepts_legacy_complete_run_without_bbox_source(self):
        run = completed_layer_a_run()
        run.pop("bbox_source")

        self.assertTrue(_has_completed_layer_a_run([run]))

    def test_completed_layer_a_run_allows_later_forest_refinement(self):
        self.assertTrue(
            _has_completed_layer_a_run(
                [
                    completed_layer_a_run(
                        run_region_geometry_hash="seed-before-refine",
                        current_region_geometry_hash="forest-mask-after-refine",
                    )
                ]
            )
        )

    def test_completed_layer_a_run_accepts_complete_bolme_shard_set(self):
        self.assertTrue(
            _has_completed_layer_a_run(
                [
                    completed_layer_a_run(
                        source="bolme",
                        bolme_inserted=5,
                        mescere_inserted=0,
                        tile_shard_count=2,
                        tile_shard_index=0,
                        bbox="same",
                    ),
                    completed_layer_a_run(
                        source="bolme",
                        bolme_inserted=7,
                        mescere_inserted=0,
                        tile_shard_count=2,
                        tile_shard_index=1,
                        bbox="same",
                    ),
                ],
                ["ogm_esatis_bolme"],
            )
        )

    def test_completed_layer_a_run_rejects_incomplete_shard_set(self):
        self.assertFalse(
            _has_completed_layer_a_run(
                [
                    completed_layer_a_run(
                        source="bolme",
                        bolme_inserted=5,
                        mescere_inserted=0,
                        tile_shard_count=2,
                        tile_shard_index=0,
                        bbox="same",
                    )
                ]
            )
        )

    def test_forest_only_grid_run_must_match_current_region(self):
        runs = [
            {
                "status": "completed",
                "total_tiles": 1,
                "processed_tiles": 1,
                "inserted_cells": 10,
                "run_region_geometry_hash": "old",
                "current_region_geometry_hash": "current",
                "forest_only_run": True,
            },
            {
                "status": "completed",
                "total_tiles": 1,
                "processed_tiles": 1,
                "inserted_cells": 10,
                "run_region_geometry_hash": "current",
                "current_region_geometry_hash": "current",
                "forest_only_run": False,
            },
        ]

        self.assertTrue(_has_completed_grid_run(runs))
        self.assertFalse(_has_forest_only_grid_run(runs))

    def test_contract_checklist_names_blockers(self):
        report = {
            "scope": {
                "acceptance_scope": "full-scale",
                "expected_grid_cells": 23000000,
                "expected_layer_a_features": 10,
            },
            "summary": {
                "grid_cells": 533,
                "layer_a_features": 3,
            },
            "schema": {
                "tables_ready": True,
                "indexes_ready": True,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_partitions": ["grid_cells"],
            },
            "readiness": {
                "source_health": "ok",
                "stage3_grid_pipeline_ready": True,
                "stage3_schema_ready": True,
                "target_grid_ready": False,
                "target_layer_a_ready": False,
                "partitioning_ready": False,
                "full_scale_partitioning_required": True,
                "carbon_foundation_ready": True,
                "carbon_calculation_ready": True,
                "carbon_audit_ready": True,
            },
        }

        checklist = _contract_checklist(report)

        self.assertFalse(checklist["ready_for_contract_acceptance"])
        self.assertIn("grid target not loaded: 533 of 23000000", checklist["blockers"])
        self.assertIn("Layer A target not loaded: 3 of 10", checklist["blockers"])
        self.assertIn("missing partitioning: grid_cells", checklist["blockers"])
        self.assertIn("Stage 3 performance report is missing or failed", checklist["blockers"])

    def test_public_ogm_scope_does_not_block_on_future_full_layer_a_target(self):
        report = {
            "scope": {
                "acceptance_scope": "public-ogm",
                "expected_grid_cells": 533,
                "expected_layer_a_features": 12,
            },
            "summary": {
                "forest_only_region": True,
                "grid_cells": 533,
                "layer_a_features": 12,
                "verification_results": 533,
                "ndvi_observations": 533,
            },
            "schema": {
                "tables_ready": True,
                "indexes_ready": True,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_partitions": ["grid_cells"],
            },
            "operational": {
                "ogm_tls_verify": True,
            },
            "readiness": {
                "bounded_workspace_ready": True,
                "source_health": "ok",
                "stage3_grid_pipeline_ready": True,
                "stage3_layer_a_pipeline_ready": True,
                "stage3_grid_run_ready": True,
                "forest_only_region_ready": True,
                "forest_mask_source_ready": True,
                "forest_only_grid_run_ready": True,
                "forest_clip_ready": True,
                "stage3_layer_a_run_ready": True,
                "performance_report_ready": True,
                "stage3_schema_ready": True,
                "target_grid_ready": True,
                "target_layer_a_ready": True,
                "partitioning_ready": False,
                "full_scale_partitioning_required": False,
                "carbon_foundation_ready": True,
                "carbon_calculation_ready": True,
                "carbon_baseline_calculation_ready": True,
                "carbon_historical_trend_ready": True,
                "carbon_forest_area_ready": True,
                "carbon_audit_ready": True,
            },
        }

        checklist = _contract_checklist(report)
        summary = _stage3_acceptance_summary({**report, "contract_checklist": checklist})

        self.assertFalse(checklist["ready_for_contract_acceptance"])
        self.assertIn(
            "public OGM/OSM scope is pilot-sized; full public forest-only rollout evidence is still required",
            checklist["blockers"],
        )
        self.assertNotIn("grid target not loaded: 533 of 23000000", checklist["blockers"])
        self.assertNotIn("missing partitioning: grid_cells", checklist["blockers"])
        self.assertTrue(checklist["items"]["official_full_layer_a_backlog"])
        self.assertFalse(checklist["items"]["public_full_scale_rollout"])
        self.assertTrue(checklist["items"]["forest_only_region"])
        self.assertTrue(checklist["items"]["forest_only_grid_run"])
        self.assertEqual(summary["status"], "public_pipeline_incomplete")
        self.assertIn("public_full_scale_rollout", summary["pending_items"])
        self.assertEqual(summary["official_full_layer_a_status"], "backlog")
        self.assertTrue(summary["forest_only_scope"])
        self.assertTrue(summary["requires_full_scale_run"])

    def test_public_scope_requires_forest_only_grid_evidence(self):
        report = {
            "scope": {"acceptance_scope": "public-ogm"},
            "summary": {
                "grid_cells": 533,
                "layer_a_features": 12,
                "verification_results": 533,
                "ndvi_observations": 533,
            },
            "schema": {
                "tables_ready": True,
                "indexes_ready": True,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_partitions": [],
            },
            "operational": {"ogm_tls_verify": True},
            "readiness": {
                "bounded_workspace_ready": True,
                "source_health": "ok",
                "stage3_grid_pipeline_ready": True,
                "stage3_layer_a_pipeline_ready": True,
                "stage3_grid_run_ready": True,
                "forest_only_region_ready": False,
                "forest_only_grid_run_ready": False,
                "stage3_layer_a_run_ready": True,
                "performance_report_ready": True,
                "stage3_schema_ready": True,
                "carbon_foundation_ready": True,
                "carbon_calculation_ready": True,
                "carbon_audit_ready": True,
            },
        }

        checklist = _contract_checklist(report)

        self.assertFalse(checklist["ready_for_contract_acceptance"])
        self.assertIn("selected Stage 3 region is not marked as forest-only", checklist["blockers"])
        self.assertIn("completed grid generation run is not marked as forest-only", checklist["blockers"])
        self.assertIn("forest clip check did not prove full 1 ha cells inside the public forest mask", checklist["blockers"])

    def test_full_scale_scope_requires_approved_carbon_calculation(self):
        summary = {
            "grid_cells": 10,
            "layer_a_features": 10,
            "verification_results": 10,
            "ndvi_observations": 10,
            "ndvi_trend_cells": 10,
            "carbon_assessments": 10,
            "calculated_carbon_assessments": 10,
            "approved_calculated_carbon_assessments": 0,
            "carbon_audit_events": 10,
            "carbon_assessments_without_audit": 0,
        }
        schema = {
            "tables_ready": True,
            "indexes_ready": True,
            "partitioning_ready": True,
            "tables": {
                "stage3_grid_generation_runs": True,
                "stage3_grid_generation_tiles": True,
                "stage3_layer_a_ingest_runs": True,
                "stage3_layer_a_ingest_tiles": True,
            },
        }

        readiness = _readiness(
            summary,
            sources=[{"source_layer": "ogm_esatis_bolme", "status": "ok"}],
            schema=schema,
            expected_grid_cells=10,
            expected_layer_a_features=10,
            grid_runs=[
                {
                    "status": "completed",
                    "total_tiles": 1,
                    "processed_tiles": 1,
                    "inserted_cells": 10,
                }
            ],
            layer_a_runs=[completed_layer_a_run(bolme_inserted=6, mescere_inserted=4)],
            expected_source_layers=["ogm_esatis_bolme"],
            performance_report_ready=True,
            acceptance_scope="full-scale",
        )
        report = {
            "scope": {
                "acceptance_scope": "full-scale",
                "expected_grid_cells": 10,
                "expected_layer_a_features": 10,
            },
            "summary": summary,
            "schema": {
                **schema,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_partitions": [],
            },
            "operational": {"ogm_tls_verify": True},
            "readiness": readiness,
        }

        checklist = _contract_checklist(report)

        self.assertTrue(readiness["carbon_calculation_ready"])
        self.assertFalse(readiness["carbon_approved_calculation_ready"])
        self.assertFalse(readiness["full_scale_ready"])
        self.assertFalse(checklist["ready_for_contract_acceptance"])
        self.assertIn("approved carbon calculations are missing for full-scale acceptance", checklist["blockers"])

    def test_future_layer_a_adapter_boundary_is_declared_in_source_strategy(self):
        self.assertTrue(_future_layer_a_adapter_boundary_ready())

    def test_acceptance_summary_separates_pipeline_from_full_scale(self):
        report = {
            "scope": {
                "acceptance_scope": "full-scale",
                "expected_grid_cells": 23000000,
                "expected_layer_a_features": 10,
            },
            "summary": {
                "grid_cells": 533,
                "layer_a_features": 3,
            },
            "readiness": {
                "stage3_schema_ready": True,
                "stage3_grid_pipeline_ready": True,
                "stage3_layer_a_pipeline_ready": True,
            },
            "contract_checklist": {
                "ready_for_contract_acceptance": False,
                "items": {
                    "ogm_layer_a_pipeline": True,
                    "full_layer_a_target_loaded": False,
                    "full_grid_target_loaded": False,
                },
            },
        }

        summary = _stage3_acceptance_summary(report)

        self.assertEqual(summary["status"], "pipeline_ready_not_loaded")
        self.assertTrue(summary["requires_full_scale_run"])
        self.assertEqual(summary["grid_progress"]["current"], 533)
        self.assertEqual(summary["grid_progress"]["target"], 23000000)
        self.assertIn("full_grid_target_loaded", summary["pending_items"])

    def test_performance_report_is_required_for_acceptance(self):
        report = {
            "scope": {
                "expected_grid_cells": 1,
                "expected_layer_a_features": 1,
            },
            "summary": {
                "grid_cells": 1,
                "layer_a_features": 1,
            },
            "schema": {
                "tables_ready": True,
                "indexes_ready": True,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_partitions": [],
            },
            "readiness": {
                "source_health": "ok",
                "stage3_grid_pipeline_ready": True,
                "stage3_layer_a_pipeline_ready": True,
                "stage3_grid_run_ready": True,
                "stage3_layer_a_run_ready": True,
                "performance_report_ready": False,
                "stage3_schema_ready": True,
                "target_grid_ready": True,
                "target_layer_a_ready": True,
                "partitioning_ready": True,
                "full_scale_partitioning_required": False,
                "carbon_foundation_ready": True,
                "carbon_calculation_ready": True,
                "carbon_audit_ready": True,
            },
        }

        checklist = _contract_checklist(report)

        self.assertFalse(checklist["ready_for_contract_acceptance"])
        self.assertFalse(checklist["items"]["performance_report"])

    def test_missing_performance_report_is_not_ready(self):
        report = _load_performance_report("/tmp/map1-missing-performance-report.json")

        self.assertFalse(report["present"])
        self.assertFalse(report["ready"])

    def test_ready_report_must_match_formula_manifest_scope(self):
        payload = {
            "region_code": "TR",
            "mask_version": "TR",
            "formula_manifest_hash": "wrong",
            "checks": {"ready": True},
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as handle:
            json.dump(payload, handle)
            handle.flush()

            report = _load_ready_report(
                handle.name,
                {
                    "region_code": "TR",
                    "mask_version": "TR",
                    "formula_manifest_hash": "expected",
                },
                "mask formula integrity",
            )

        self.assertFalse(report["checks"]["ready"])
        self.assertFalse(report["scope_matches"])

    def test_performance_report_must_match_scope_and_include_thresholds(self):
        payload = {
            "status": "ok",
            "scope": {
                "region_code": "TR_BOLU_TEST",
                "grid_version": "stage3-v1",
            },
            "max_avg_ms": 250,
            "max_p95_ms": 500,
            "probes": [
                {"name": name, "avg_ms": 10, "p95_ms": 12, "plan": ["Index Scan"]}
                for name in sorted(REQUIRED_PERFORMANCE_PROBES)
            ],
        }
        for probe in payload["probes"]:
            probe["result_count"] = 1
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as handle:
            json.dump(payload, handle)
            handle.flush()

            report = _load_performance_report(
                handle.name,
                {
                    "region_code": "TR_BOLU_TEST",
                    "grid_version": "stage3-v1",
                },
                {
                    "grid_cells": 0,
                },
            )
            wrong_scope = _load_performance_report(
                handle.name,
                {
                    "region_code": "OTHER",
                    "grid_version": "stage3-v1",
                },
            )

        self.assertTrue(report["ready"])
        self.assertTrue(report["scope_matches"])
        self.assertTrue(report["table_counts_match"])
        self.assertTrue(report["thresholds_present"])
        self.assertTrue(report["required_probes_present"])
        self.assertTrue(report["non_empty_probes"])
        self.assertTrue(report["explain_present"])
        self.assertFalse(wrong_scope["ready"])
        self.assertFalse(wrong_scope["scope_matches"])

    def test_performance_report_rejects_stale_table_counts(self):
        payload = {
            "status": "ok",
            "scope": {
                "region_code": "TR_BOLU_TEST",
                "grid_version": "stage3-v1",
            },
            "table_counts": {
                "grid_cells": 533,
                "layer_a_features": 12,
                "ndvi_observations": 533,
                "verification_results": 533,
            },
            "max_avg_ms": 250,
            "max_p95_ms": 500,
            "probes": [
                {"name": name, "avg_ms": 10, "p95_ms": 12, "result_count": 1, "plan": ["Index Scan"]}
                for name in sorted(REQUIRED_PERFORMANCE_PROBES)
            ],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as handle:
            json.dump(payload, handle)
            handle.flush()

            report = _load_performance_report(
                handle.name,
                {
                    "region_code": "TR_BOLU_TEST",
                    "grid_version": "stage3-v1",
                },
                {
                    "grid_cells": 534,
                    "layer_a_features": 12,
                },
            )

        self.assertFalse(report["ready"])
        self.assertFalse(report["table_counts_match"])

    def test_performance_report_rejects_missing_required_probes(self):
        payload = {
            "status": "ok",
            "scope": {
                "region_code": "TR_BOLU_TEST",
                "grid_version": "stage3-v1",
            },
            "max_avg_ms": 250,
            "max_p95_ms": 500,
            "probes": [
                {
                    "name": "asset_register_page",
                    "avg_ms": 10,
                    "p95_ms": 12,
                    "plan": ["Index Scan"],
                }
            ],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as handle:
            json.dump(payload, handle)
            handle.flush()

            report = _load_performance_report(
                handle.name,
                {
                    "region_code": "TR_BOLU_TEST",
                    "grid_version": "stage3-v1",
                },
            )

        self.assertFalse(report["ready"])
        self.assertFalse(report["required_probes_present"])
        self.assertIn("layer_c_tile_window", report["missing_probes"])

    def test_performance_report_rejects_empty_required_probe(self):
        payload = {
            "status": "ok",
            "scope": {
                "region_code": "TR_BOLU_TEST",
                "grid_version": "stage3-v1",
            },
            "max_avg_ms": 250,
            "max_p95_ms": 500,
            "probes": [
                {"name": name, "avg_ms": 10, "p95_ms": 12, "result_count": 1, "plan": ["Index Scan"]}
                for name in sorted(REQUIRED_PERFORMANCE_PROBES)
            ],
        }
        payload["probes"][-1]["result_count"] = 0
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as handle:
            json.dump(payload, handle)
            handle.flush()

            report = _load_performance_report(
                handle.name,
                {
                    "region_code": "TR_BOLU_TEST",
                    "grid_version": "stage3-v1",
                },
            )

        self.assertFalse(report["ready"])
        self.assertTrue(report["required_probes_present"])
        self.assertFalse(report["non_empty_probes"])

    def test_performance_report_rejects_probe_threshold_breach(self):
        payload = {
            "status": "ok",
            "scope": {
                "region_code": "TR_BOLU_TEST",
                "grid_version": "stage3-v1",
            },
            "max_avg_ms": 250,
            "max_p95_ms": 500,
            "probes": [
                {"name": name, "avg_ms": 10, "p95_ms": 12, "result_count": 1, "plan": ["Index Scan"]}
                for name in sorted(REQUIRED_PERFORMANCE_PROBES)
            ],
        }
        payload["probes"][0]["avg_ms"] = 300
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as handle:
            json.dump(payload, handle)
            handle.flush()

            report = _load_performance_report(
                handle.name,
                {
                    "region_code": "TR_BOLU_TEST",
                    "grid_version": "stage3-v1",
                },
            )

        self.assertFalse(report["ready"])
        self.assertFalse(report["thresholds_pass"])

    def test_completed_run_helpers_reject_partial_or_failed_runs(self):
        self.assertFalse(
            _has_completed_grid_run(
                [
                    {
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 9,
                        "inserted_cells": 100,
                    }
                ]
            )
        )
        self.assertFalse(
            _has_completed_grid_run(
                [
                    {
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "inserted_cells": 100,
                        "run_region_geometry_hash": "old",
                        "current_region_geometry_hash": "new",
                    }
                ]
            )
        )
        self.assertTrue(
            _has_completed_grid_run(
                [
                    {
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "inserted_cells": 100,
                        "run_region_geometry_hash": "same",
                        "current_region_geometry_hash": "same",
                    }
                ]
            )
        )
        self.assertFalse(
            _has_forest_only_grid_run(
                [
                    {
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "inserted_cells": 100,
                        "forest_only_run": True,
                        "run_region_geometry_hash": "same",
                        "current_region_geometry_hash": "same",
                    }
                ],
                require_component_mask=True,
            )
        )
        self.assertTrue(
            _has_forest_only_grid_run(
                [
                    {
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "inserted_cells": 100,
                        "forest_only_run": True,
                        "grid_mask_method": "region_mask_components",
                        "mask_version": "TR-STAGE3-PUBLIC-OSM-WC-FOREST-V4",
                        "run_region_geometry_hash": "same",
                        "current_region_geometry_hash": "same",
                    }
                ],
                require_component_mask=True,
            )
        )
        self.assertFalse(
            _has_completed_grid_run(
                [
                    {
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "inserted_cells": 0,
                    }
                ]
            )
        )
        self.assertFalse(
            _has_completed_layer_a_run(
                [
                    {
                        "source": "all",
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "bolme_inserted": 3,
                        "mescere_inserted": 0,
                        "failed_bolme": 1,
                        "failed_mescere": 0,
                    }
                ]
            )
        )
        self.assertFalse(
            _has_completed_layer_a_run(
                [
                    {
                        "source": "bolme",
                        "status": "completed",
                        "total_tiles": 10,
                        "processed_tiles": 10,
                        "bolme_inserted": 3,
                        "mescere_inserted": 0,
                        "failed_bolme": 0,
                        "failed_mescere": 0,
                    }
                ]
            )
        )
        self.assertTrue(
            _has_completed_layer_a_run(
                [
                    completed_layer_a_run(
                        total_tiles=10,
                        processed_tiles=10,
                        bolme_inserted=3,
                        mescere_inserted=2,
                        run_region_geometry_hash="seed-before-refine",
                        current_region_geometry_hash="forest-mask-after-refine",
                    )
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
