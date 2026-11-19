import unittest

from app.api.stage1 import stage1_params


class _ScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._values.pop(0) if self._values else None


class _Db:
    def __init__(self, values):
        self._values = list(values)

    def execute(self, *_args, **_kwargs):
        return _ScalarResult([self._values.pop(0)])


class Stage1ParamsTest(unittest.TestCase):
    def test_public_clean_region_uses_region_forest_mask_source_layers(self):
        params = stage1_params(
            region_code="TR-STAGE3-PUBLIC-OSM-WC-FOREST",
            grid_version="stage3-public-osm-wc-clean-v3",
            db=_Db([
                ["osm_forest_mask"],
                "TR-STAGE3-PUBLIC-FULL",
            ]),
        )

        self.assertEqual(params["layer_a_region_code"], "TR-STAGE3-PUBLIC-FULL")
        self.assertEqual(params["layer_a_source_layers"], ["osm_forest_mask"])
        self.assertEqual(params["forest_mask_source_layers"], ["osm_forest_mask"])


if __name__ == "__main__":
    unittest.main()
