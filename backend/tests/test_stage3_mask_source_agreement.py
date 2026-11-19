import inspect
import unittest

from app.commands.check_stage3_mask_source_agreement import (
    _parse_bbox,
    mask_source_agreement_report,
)


class MaskSourceAgreementDb:
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


class Stage3MaskSourceAgreementTest(unittest.TestCase):
    def test_parse_bbox_requires_four_ordered_values(self):
        self.assertEqual(_parse_bbox("32.05,36.49,32.24,36.61"), (32.05, 36.49, 32.24, 36.61))
        with self.assertRaises(ValueError):
            _parse_bbox("32,36,31,37")

    def test_report_fails_when_source_loss_exceeds_budget(self):
        report = mask_source_agreement_report(
            MaskSourceAgreementDb(
                {
                    "forest_source_components": 10,
                    "forest_source_area_m2": 100_000,
                    "final_mask_components": 8,
                    "final_mask_area_m2": 80_000,
                    "exclusion_components": 2,
                    "exclusion_area_m2": 5_000,
                    "retained_forest_source_area_m2": 80_000,
                    "excluded_forest_source_area_m2": 5_000,
                    "unexplained_removed_area_m2": 15_000,
                    "unexplained_removed_components": 1,
                    "max_unexplained_removed_m2": 15_000,
                    "exclusion_layers": {},
                }
            ),
            region_code="TR-FOREST",
            source_region_code="TR-SOURCE",
            mask_version="TR-FOREST",
            forest_source_layer="osm_forest_mask",
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            max_unexplained_ratio=0.01,
            max_unexplained_component_ha=1.0,
        )

        self.assertFalse(report["checks"]["unexplained_source_loss_within_ratio_budget"])
        self.assertFalse(report["checks"]["unexplained_source_loss_within_component_budget"])
        self.assertFalse(report["checks"]["ready"])
        self.assertEqual(report["unexplained_removed_components"], 1)

    def test_report_accepts_explained_source_loss(self):
        report = mask_source_agreement_report(
            MaskSourceAgreementDb(
                {
                    "forest_source_components": 10,
                    "forest_source_area_m2": 100_000,
                    "final_mask_components": 8,
                    "final_mask_area_m2": 80_000,
                    "exclusion_components": 2,
                    "exclusion_area_m2": 20_000,
                    "retained_forest_source_area_m2": 80_000,
                    "excluded_forest_source_area_m2": 20_000,
                    "unexplained_removed_area_m2": 0,
                    "unexplained_removed_components": 0,
                    "max_unexplained_removed_m2": 0,
                    "exclusion_layers": {},
                }
            ),
            region_code="TR-FOREST",
            source_region_code="TR-SOURCE",
            mask_version="TR-FOREST",
            forest_source_layer="osm_forest_mask",
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
        )

        self.assertTrue(report["checks"]["ready"])
        self.assertEqual(report["unexplained_removed_area_ha"], 0)

    def test_report_accepts_minor_boundary_source_loss_within_budget(self):
        report = mask_source_agreement_report(
            MaskSourceAgreementDb(
                {
                    "forest_source_components": 10,
                    "forest_source_area_m2": 1_000_000,
                    "final_mask_components": 8,
                    "final_mask_area_m2": 990_000,
                    "exclusion_components": 2,
                    "exclusion_area_m2": 5_000,
                    "retained_forest_source_area_m2": 990_000,
                    "excluded_forest_source_area_m2": 5_000,
                    "unexplained_removed_area_m2": 5_000,
                    "unexplained_removed_components": 1,
                    "max_unexplained_removed_m2": 5_000,
                    "exclusion_layers": {},
                }
            ),
            region_code="TR-FOREST",
            source_region_code="TR-SOURCE",
            mask_version="TR-FOREST",
            forest_source_layer="osm_forest_mask",
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            max_unexplained_ratio=0.01,
            max_unexplained_component_ha=1.0,
        )

        self.assertTrue(report["checks"]["ready"])
        self.assertEqual(report["unexplained_removed_components"], 1)

    def test_report_rejects_exact_component_budget_loss(self):
        report = mask_source_agreement_report(
            MaskSourceAgreementDb(
                {
                    "forest_source_components": 10,
                    "forest_source_area_m2": 1_000_000,
                    "final_mask_components": 8,
                    "final_mask_area_m2": 990_000,
                    "exclusion_components": 2,
                    "exclusion_area_m2": 0,
                    "retained_forest_source_area_m2": 990_000,
                    "excluded_forest_source_area_m2": 0,
                    "unexplained_removed_area_m2": 10_000,
                    "unexplained_removed_components": 1,
                    "max_unexplained_removed_m2": 10_000,
                    "exclusion_layers": {},
                }
            ),
            region_code="TR-FOREST",
            source_region_code="TR-SOURCE",
            mask_version="TR-FOREST",
            forest_source_layer="osm_forest_mask",
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            max_unexplained_ratio=0.02,
            max_unexplained_component_ha=1.0,
        )

        self.assertFalse(report["checks"]["unexplained_source_loss_within_component_budget"])
        self.assertFalse(report["checks"]["ready"])

    def test_report_records_fragment_filter_loss_separately(self):
        report = mask_source_agreement_report(
            MaskSourceAgreementDb(
                {
                    "forest_source_components": 10,
                    "forest_source_area_m2": 1_000_000,
                    "final_mask_components": 8,
                    "final_mask_area_m2": 900_000,
                    "exclusion_components": 2,
                    "exclusion_area_m2": 50_000,
                    "retained_forest_source_area_m2": 900_000,
                    "excluded_forest_source_area_m2": 50_000,
                    "unexplained_removed_area_m2": 0,
                    "unexplained_removed_components": 0,
                    "max_unexplained_removed_m2": 0,
                    "explained_fragment_removed_area_m2": 50_000,
                    "explained_fragment_removed_components": 12,
                    "exclusion_layers": {},
                }
            ),
            region_code="TR-FOREST",
            source_region_code="TR-SOURCE",
            mask_version="TR-FOREST",
            forest_source_layer="osm_forest_mask",
            exclusion_source_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
        )

        self.assertTrue(report["checks"]["ready"])
        self.assertEqual(report["unexplained_removed_area_ha"], 0)
        self.assertEqual(report["explained_fragment_removed_area_ha"], 5.0)
        self.assertEqual(report["explained_fragment_removed_components"], 12)

    def test_query_compares_source_final_mask_and_exclusions(self):
        db = MaskSourceAgreementDb(
            {
                "forest_source_components": 1,
                "forest_source_area_m2": 10_000,
                "final_mask_components": 1,
                "final_mask_area_m2": 10_000,
                "exclusion_components": 1,
                "exclusion_area_m2": 0,
                "retained_forest_source_area_m2": 10_000,
                "excluded_forest_source_area_m2": 0,
                "unexplained_removed_area_m2": 0,
                "unexplained_removed_components": 0,
                "max_unexplained_removed_m2": 0,
                "exclusion_layers": {},
            }
        )

        mask_source_agreement_report(
            db,
            region_code="TR-FOREST",
            source_region_code="TR-SOURCE",
            mask_version="TR-FOREST",
            forest_source_layer="osm_forest_mask",
            exclusion_source_layers=["osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
            projected_srid=6933,
            bbox=(32.05, 36.49, 32.24, 36.61),
            component_limit=100,
            component_offset=200,
        )

        self.assertIn("stage3_source_mask_cache", db.sql)
        self.assertIn("stage3_region_mask_components", db.sql)
        self.assertIn("material_loss_m2", db.sql)
        self.assertIn("fragment_loss_m2", db.sql)
        self.assertIn("global_loss_metrics", db.sql)
        self.assertIn("global_unexplained_removed_area_m2", db.sql)
        self.assertIn("material_component_area_m2", db.sql)
        self.assertIn("min_disagreement_area_m2", db.sql)
        self.assertIn("WHERE area_m2 >= (SELECT material_component_area_m2 FROM params)", db.sql)
        self.assertIn("WHERE area_m2 < (SELECT material_component_area_m2 FROM params)", db.sql)
        self.assertIn("ST_SnapToGrid", db.sql)
        self.assertIn("LIMIT NULLIF(:component_limit, 0)", db.sql)
        self.assertEqual(db.params["use_bbox"], True)
        self.assertEqual(db.params["component_limit"], 100)
        self.assertEqual(db.params["component_offset"], 200)


if __name__ == "__main__":
    unittest.main()
