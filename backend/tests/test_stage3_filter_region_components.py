import unittest

from app.commands.filter_stage3_region_components import (
    _guard_legacy_filter_not_full,
    _is_legacy_filter_full_scope,
    _target_metadata,
)
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_SOURCE_REGION_CODE,
    STAGE3_PUBLIC_V9_GRID_VERSION,
    STAGE3_PUBLIC_V9_REGION_CODE,
)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class MetadataDb:
    def __init__(self, metadata):
        self.metadata = metadata
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))
        return Result(self.metadata)


class Stage3FilterRegionComponentsTest(unittest.TestCase):
    def test_legacy_filter_detects_full_v9_identifiers_and_alias_area(self):
        self.assertTrue(
            _is_legacy_filter_full_scope(
                source_region_code="TR-PILOT",
                target_region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                mask_version="pilot-mask",
            )
        )
        self.assertTrue(
            _is_legacy_filter_full_scope(
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                target_region_code="TR-PILOT",
                mask_version="pilot-mask",
            )
        )
        self.assertTrue(
            _is_legacy_filter_full_scope(
                source_region_code="TR-TURKEY-ALIAS",
                target_region_code="TR-TURKEY-ALIAS-FILTERED",
                mask_version=STAGE3_PUBLIC_V9_GRID_VERSION,
            )
        )
        self.assertTrue(
            _is_legacy_filter_full_scope(
                source_region_code="TR-TURKEY-ALIAS",
                target_region_code="TR-TURKEY-ALIAS-FILTERED",
                mask_version="alias-mask",
                scope_area_ha=1_250_000,
            )
        )
        self.assertFalse(
            _is_legacy_filter_full_scope(
                source_region_code="TR-PILOT",
                target_region_code="TR-PILOT-FILTERED",
                mask_version="pilot-mask",
                scope_area_ha=533,
            )
        )

    def test_legacy_filter_guard_rejects_canonical_target_before_db(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy Stage 3 component filtering is disabled"):
            _guard_legacy_filter_not_full(
                object(),
                source_region_code="TR-PILOT",
                target_region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                mask_version="pilot-mask",
            )

    def test_legacy_filter_guard_rejects_national_alias_source_area(self):
        db = MetadataDb(1_250_000)

        with self.assertRaisesRegex(RuntimeError, "Legacy Stage 3 component filtering is disabled"):
            _guard_legacy_filter_not_full(
                db,
                source_region_code="TR-TURKEY-ALIAS",
                target_region_code="TR-TURKEY-ALIAS-FILTERED",
                mask_version="alias-mask",
            )

    def test_target_metadata_records_final_component_filter_policy(self):
        db = MetadataDb(
            {
                "forest_only": True,
                "forest_mask": {
                    "source_region_code": "TR-STAGE3-PUBLIC-FULL",
                    "source_layers": ["osm_forest_mask"],
                    "difference_source_layers": [
                        "osm_nonforest_exclusion",
                        "esa_worldcover_nonforest_exclusion",
                    ],
                    "method": "public_osm_forest_mask_geometry_and_nonforest_difference",
                },
            }
        )

        metadata = _target_metadata(
            db,
            source_region_code="TR-CLEAN-V3",
            target_region_code="TR-CLEAN-V4",
            min_component_area_ha=1.0,
        )

        policy = metadata["forest_mask"]["clean_mask_policy"]
        self.assertEqual(metadata["stage3_region_source"], "filtered_final_forest_mask_components")
        self.assertEqual(metadata["forest_mask"]["filtered_from_region_code"], "TR-CLEAN-V3")
        self.assertEqual(metadata["forest_mask"]["filtered_target_region_code"], "TR-CLEAN-V4")
        self.assertEqual(policy["source_geometry"], "final_post_difference_region_components")
        self.assertEqual(policy["min_component_area_ha"], 1.0)
        self.assertTrue(policy["applies_before_grid_insert"])
        self.assertIn("final_component_filter", metadata["forest_mask"]["method"])


if __name__ == "__main__":
    unittest.main()
