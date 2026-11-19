import json
import tempfile
import unittest
from pathlib import Path

from app.commands.run_stage3_official_layer_a_import import load_geojson_features


class Stage3OfficialVectorImportTest(unittest.TestCase):
    def test_loads_feature_collection_with_selected_source_id(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"parcel_code": "A-1", "forest_type": "pine"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [30.0, 40.0],
                                [30.0, 40.1],
                                [30.1, 40.1],
                                [30.1, 40.0],
                                [30.0, 40.0],
                            ]
                        ],
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layer-a.geojson"
            path.write_text(json.dumps(payload), encoding="utf-8")

            features = load_geojson_features(path, source_id_property="parcel_code")

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].source_id, "A-1")
        self.assertEqual(features[0].attributes["forest_type"], "pine")

    def test_loads_single_feature_with_fallback_id(self):
        payload = {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [30.0, 40.0],
                        [30.0, 40.1],
                        [30.1, 40.1],
                        [30.1, 40.0],
                        [30.0, 40.0],
                    ]
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layer-a.geojson"
            path.write_text(json.dumps(payload), encoding="utf-8")

            features = load_geojson_features(path)

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].source_id, "official-1")

    def test_rejects_plain_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "geometry.geojson"
            path.write_text(json.dumps({"type": "Polygon", "coordinates": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "FeatureCollection or Feature"):
                load_geojson_features(path)


if __name__ == "__main__":
    unittest.main()
