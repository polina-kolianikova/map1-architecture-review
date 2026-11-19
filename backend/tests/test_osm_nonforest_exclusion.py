import unittest
from unittest.mock import Mock, patch

import requests

from app.commands.ingest_osm_nonforest_exclusion import fetch_osm_elements, osm_exclusion_features


class OsmNonforestExclusionTest(unittest.TestCase):
    def test_highway_way_becomes_buffered_line_feature(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 21,
                    "tags": {"highway": "track"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["source_id"], "osm:way:21")
        self.assertEqual(features[0]["exclusion_type"], "road")
        self.assertEqual(features[0]["buffer_m"], 8)
        self.assertEqual(features[0]["geometry"]["type"], "LineString")

    def test_closed_building_way_becomes_polygon_feature(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 22,
                    "tags": {"building": "yes"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                        {"lon": 30.0, "lat": 40.0},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["source_id"], "osm:way:22")
        self.assertEqual(features[0]["exclusion_type"], "building")
        self.assertEqual(features[0]["buffer_m"], 0)
        self.assertEqual(features[0]["geometry"]["type"], "Polygon")

    def test_closed_amenity_way_becomes_polygon_feature(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 23,
                    "tags": {"amenity": "school"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                        {"lon": 30.0, "lat": 40.0},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["source_id"], "osm:way:23")
        self.assertEqual(features[0]["exclusion_type"], "amenity")
        self.assertEqual(features[0]["buffer_m"], 0)
        self.assertEqual(features[0]["geometry"]["type"], "Polygon")

    def test_closed_barrier_way_becomes_polygon_feature(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 25,
                    "tags": {"barrier": "fence"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                        {"lon": 30.0, "lat": 40.0},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["source_id"], "osm:way:25")
        self.assertEqual(features[0]["exclusion_type"], "barrier")
        self.assertEqual(features[0]["buffer_m"], 0)
        self.assertEqual(features[0]["geometry"]["type"], "Polygon")

    def test_nature_reserve_is_not_nonforest_exclusion(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 26,
                    "tags": {"leisure": "nature_reserve", "name": "Protected forest"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                        {"lon": 30.0, "lat": 40.0},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(features, [])

    def test_explicit_leisure_pitch_is_nonforest_exclusion(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 27,
                    "tags": {"leisure": "pitch"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                        {"lon": 30.0, "lat": 40.0},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["exclusion_type"], "leisure")

    def test_waterway_way_becomes_buffered_line_feature(self):
        features = osm_exclusion_features(
            [
                {
                    "type": "way",
                    "id": 24,
                    "tags": {"waterway": "stream"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                    ],
                }
            ],
            line_buffer_m=8,
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["exclusion_type"], "waterway")
        self.assertEqual(features[0]["buffer_m"], 8)

    def test_fetch_osm_elements_tries_next_endpoint_after_http_error(self):
        failed = Mock()
        failed.raise_for_status.side_effect = requests.HTTPError("timeout")
        ok = Mock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"elements": [{"type": "way", "id": 21}]}

        with patch("app.commands.ingest_osm_nonforest_exclusion.requests.post", side_effect=[failed, ok]) as post:
            elements = fetch_osm_elements(["https://a.test", "https://b.test"], (30, 40, 31, 41), 20)

        self.assertEqual(elements, [{"type": "way", "id": 21}])
        self.assertEqual(post.call_count, 2)

    def test_fetch_osm_elements_retries_without_relations_after_endpoint_failures(self):
        failed_a = Mock()
        failed_a.raise_for_status.side_effect = requests.HTTPError("timeout-a")
        failed_b = Mock()
        failed_b.raise_for_status.side_effect = requests.HTTPError("timeout-b")
        ok = Mock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"elements": [{"type": "way", "id": 22}]}

        with patch(
            "app.commands.ingest_osm_nonforest_exclusion.requests.post",
            side_effect=[failed_a, failed_b, ok],
        ) as post:
            elements = fetch_osm_elements(["https://a.test", "https://b.test"], (30, 40, 31, 41), 20)

        self.assertEqual(elements, [{"type": "way", "id": 22}])
        self.assertEqual(post.call_count, 3)
        self.assertIn('relation["building"]', post.call_args_list[0].kwargs["data"]["data"])
        self.assertIn('way["amenity"]', post.call_args_list[0].kwargs["data"]["data"])
        self.assertIn('way["barrier"]', post.call_args_list[0].kwargs["data"]["data"])
        self.assertIn('way["waterway"]', post.call_args_list[0].kwargs["data"]["data"])
        self.assertNotIn('relation["building"]', post.call_args_list[2].kwargs["data"]["data"])


if __name__ == "__main__":
    unittest.main()
