import unittest

from app.services.ogm_webmap import parse_webmap_layers, parse_webmap_service_candidates


class OgmWebMapParserTest(unittest.TestCase):
    def test_parse_parent_and_child_layers(self):
        payload = {
            "operationalLayers": [
                {
                    "id": "forest-assets",
                    "title": "Forest Assets",
                    "url": "https://example.test/arcgis/rest/services/forest/FeatureServer",
                    "layerType": "ArcGISFeatureLayer",
                    "visibility": True,
                    "layers": [
                        {
                            "id": 0,
                            "name": "Compartments",
                            "visibility": False,
                        }
                    ],
                }
            ]
        }

        layers = parse_webmap_layers(payload)

        self.assertEqual(len(layers), 2)
        self.assertEqual(layers[0].layer_id, "forest-assets")
        self.assertEqual(layers[0].title, "Forest Assets")
        self.assertEqual(layers[1].parent_id, "forest-assets")
        self.assertEqual(layers[1].layer_id, "0")
        self.assertEqual(layers[1].title, "Compartments")
        self.assertEqual(layers[1].url, "https://example.test/arcgis/rest/services/forest/FeatureServer")
        self.assertFalse(layers[1].visible)

    def test_parse_nested_webmap_layers(self):
        payload = {
            "operationalLayers": [
                {
                    "id": "root",
                    "title": "Root",
                    "url": "https://example.test/arcgis/rest/services/forest/MapServer",
                    "sublayers": [
                        {
                            "id": "group",
                            "title": "Group",
                            "sublayers": [
                                {
                                    "id": 2,
                                    "name": "Nested Compartments",
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        layers = parse_webmap_layers(payload)

        self.assertEqual([layer.layer_id for layer in layers], ["root", "group", "2"])
        self.assertEqual(layers[2].parent_id, "group")
        self.assertEqual(layers[2].url, "https://example.test/arcgis/rest/services/forest/MapServer")

    def test_build_arcgis_query_candidates(self):
        payload = {
            "operationalLayers": [
                {
                    "id": "forest",
                    "title": "Forest",
                    "url": "https://example.test/arcgis/rest/services/forest/MapServer",
                    "layers": [
                        {
                            "id": "client-layer-id",
                            "layerId": 3,
                            "name": "Compartments",
                        }
                    ],
                },
                {
                    "id": "ready-layer",
                    "title": "Ready Layer",
                    "url": "https://example.test/arcgis/rest/services/ready/FeatureServer/0",
                },
            ]
        }

        candidates = parse_webmap_service_candidates(payload)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].layer_url, "https://example.test/arcgis/rest/services/forest/MapServer/3")
        self.assertEqual(candidates[0].query_url, "https://example.test/arcgis/rest/services/forest/MapServer/3/query")
        self.assertEqual(candidates[1].layer_url, "https://example.test/arcgis/rest/services/ready/FeatureServer/0")

    def test_build_arcgis_query_candidate_for_child_layer_zero(self):
        payload = {
            "operationalLayers": [
                {
                    "id": "forest",
                    "title": "Forest",
                    "url": "https://example.test/arcgis/rest/services/forest/MapServer",
                    "layers": [
                        {
                            "id": 0,
                            "name": "Compartments",
                        }
                    ],
                }
            ]
        }

        candidates = parse_webmap_service_candidates(payload)

        self.assertEqual(candidates[0].layer_id, "0")
        self.assertEqual(candidates[0].layer_url, "https://example.test/arcgis/rest/services/forest/MapServer/0")

    def test_build_wms_candidate(self):
        payload = {
            "operationalLayers": [
                {
                    "id": "mescere",
                    "title": "Mescere",
                    "url": "https://example.test/geoserver/ogm/wms",
                    "layerType": "WMS",
                }
            ]
        }

        candidates = parse_webmap_service_candidates(payload)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].layer_id, "mescere")
        self.assertEqual(candidates[0].query_url, "https://example.test/geoserver/ogm/wms")

    def test_ignores_unexpected_layer_shapes(self):
        layers = parse_webmap_layers({"operationalLayers": [None, "bad", {"title": "Valid"}]})

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0].layer_id, "Valid")


if __name__ == "__main__":
    unittest.main()
