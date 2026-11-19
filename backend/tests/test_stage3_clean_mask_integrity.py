import unittest

from app.commands.check_stage3_clean_mask_integrity import (
    _parse_layers,
    clean_mask_integrity_report,
)
from app.services.stage3_public_v9 import stage3_public_v9_manifest_hash


class CleanMaskIntegrityDb:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = {}

    def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = params or {}
        return self

    def mappings(self):
        return self

    def one(self):
        return self.row


def base_row(**overrides):
    row = {
        "component_count": 10,
        "empty_components": 0,
        "invalid_components": 0,
        "tiny_components": 0,
        "component_area_m2": 100_000,
        "exclusion_overlap_area_m2": 0,
        "components_with_exclusion_overlap": 0,
        "max_component_exclusion_overlap_m2": 0,
        "outside_source_area_m2": 0,
        "components_outside_source": 0,
        "max_component_outside_source_m2": 0,
        "outside_source_region_area_m2": 0,
        "components_outside_source_region": 0,
        "max_component_outside_source_region_m2": 0,
        "source_missing_component_count": 0,
        "source_missing_large_component_count": 0,
        "source_missing_area_m2": 0,
        "source_missing_large_area_m2": 0,
        "max_source_missing_component_m2": 0,
        "live_source_rows": 20,
        "live_source_rows_missing_cache": 0,
        "stale_cache_rows": 0,
        "source_cache_pre_filtered_rows": 0,
    }
    row.update(overrides)
    return row


class Stage3CleanMaskIntegrityTest(unittest.TestCase):
    def test_parse_layers_trims_empty_values(self):
        self.assertEqual(_parse_layers("ogm_esatis_bolme, ,osm_forest_mask"), ["ogm_esatis_bolme", "osm_forest_mask"])

    def test_report_is_ready_when_strict_geometry_and_cache_checks_pass(self):
        report = clean_mask_integrity_report(
            CleanMaskIntegrityDb(base_row()),
            region_code="TR-V8",
            source_region_code="TR-SOURCE",
            mask_version="TR-V8",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            intersect_source_layers=[],
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
        )

        self.assertTrue(report["checks"]["ready"])
        self.assertEqual(report["component_area_ha"], 10.0)
        self.assertTrue(report["checks"]["client_formula_manifest_matches"])
        self.assertEqual(report["formula_manifest_hash"], stage3_public_v9_manifest_hash())

    def test_report_rejects_non_client_formula_manifest(self):
        report = clean_mask_integrity_report(
            CleanMaskIntegrityDb(base_row()),
            region_code="TR-V8",
            source_region_code="TR-SOURCE",
            mask_version="TR-V8",
            source_layers=["osm_forest_mask"],
            intersect_source_layers=[],
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
        )

        self.assertFalse(report["checks"]["client_formula_manifest_matches"])
        self.assertEqual(report["formula_manifest_hash"], "")
        self.assertFalse(report["checks"]["ready"])

    def test_report_rejects_exclusion_overlap_and_stale_cache(self):
        report = clean_mask_integrity_report(
            CleanMaskIntegrityDb(
                base_row(
                    exclusion_overlap_area_m2=3.5,
                    components_with_exclusion_overlap=1,
                    live_source_rows_missing_cache=2,
                    stale_cache_rows=1,
                    source_cache_pre_filtered_rows=3,
                )
            ),
            region_code="TR-V8",
            source_region_code="TR-SOURCE",
            mask_version="TR-V8",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            intersect_source_layers=[],
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            max_overlap_area_m2=1.0,
        )

        self.assertFalse(report["checks"]["no_component_exclusion_overlap"])
        self.assertFalse(report["checks"]["source_cache_complete"])
        self.assertFalse(report["checks"]["source_cache_current"])
        self.assertFalse(report["checks"]["source_cache_unfiltered"])
        self.assertFalse(report["checks"]["ready"])

    def test_report_rejects_missing_source_component_at_min_area(self):
        report = clean_mask_integrity_report(
            CleanMaskIntegrityDb(
                base_row(
                    source_missing_component_count=3,
                    source_missing_large_component_count=1,
                    source_missing_area_m2=12_000,
                    source_missing_large_area_m2=10_500,
                    max_source_missing_component_m2=10_500,
                )
            ),
            region_code="TR-V8",
            source_region_code="TR-SOURCE",
            mask_version="TR-V8",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            intersect_source_layers=[],
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
        )

        self.assertFalse(report["checks"]["no_missing_source_components_at_or_above_min_area"])
        self.assertEqual(report["source_missing_large_component_count"], 1)
        self.assertFalse(report["checks"]["ready"])

    def test_query_checks_exclusions_source_union_and_cache_freshness(self):
        db = CleanMaskIntegrityDb(base_row())

        clean_mask_integrity_report(
            db,
            region_code="TR-V8",
            source_region_code="TR-SOURCE",
            mask_version="TR-V8",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            intersect_source_layers=[],
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
        )

        self.assertIn("exclusion_cache", db.sql)
        self.assertIn("source_cache", db.sql)
        self.assertIn("intersect_cache", db.sql)
        self.assertIn("analysis_region", db.sql)
        self.assertIn("source_freshness", db.sql)
        self.assertIn("stale_cache", db.sql)
        self.assertIn("filtered_source_cache", db.sql)
        self.assertIn("clean_mask_policy", db.sql)
        self.assertIn("source_residual_pieces", db.sql)
        self.assertIn("source_missing_metrics", db.sql)
        self.assertIn("FROM ogm_parcels parcel, params, analysis_region", db.sql)
        self.assertIn("ST_Intersection(", db.sql)
        self.assertIn("ST_Difference(component.geom_projected, formula_base.geom)", db.sql)
        self.assertIn("ST_Difference(", db.sql)
        self.assertEqual(db.params["source_layers"], ["ogm_esatis_bolme", "osm_forest_mask"])
        self.assertEqual(db.params["intersect_source_layers"], [])


if __name__ == "__main__":
    unittest.main()
