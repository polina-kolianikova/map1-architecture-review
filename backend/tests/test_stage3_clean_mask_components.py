import unittest

from app.commands.check_stage3_clean_mask_components import _component_checks


class Stage3CleanMaskComponentsTest(unittest.TestCase):
    def test_clean_mask_component_checks_require_no_tiny_final_components(self):
        row = {
            "component_count": 25,
            "invalid_components": 0,
            "empty_components": 0,
            "components_below_min_area": 3,
        }
        policy = {
            "method": "component_area_filter_before_grid_generation",
            "min_component_area_ha": 1.0,
            "applies_before_grid_insert": True,
        }

        checks = _component_checks(row, clean_policy=policy, min_component_area_ha=1.0)

        self.assertFalse(checks["no_components_below_min_area"])
        self.assertFalse(checks["ready"])

    def test_clean_mask_component_checks_accept_final_component_policy(self):
        row = {
            "component_count": 25,
            "invalid_components": 0,
            "empty_components": 0,
            "components_below_min_area": 0,
        }
        policy = {
            "method": "component_area_filter_before_grid_generation",
            "min_component_area_ha": 1.0,
            "applies_before_grid_insert": True,
        }

        checks = _component_checks(row, clean_policy=policy, min_component_area_ha=1.0)

        self.assertTrue(checks["ready"])


if __name__ == "__main__":
    unittest.main()
