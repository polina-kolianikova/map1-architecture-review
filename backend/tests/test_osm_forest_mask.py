import unittest
from unittest.mock import Mock, patch

import requests

from app.commands.ingest_osm_forest_mask import (
    _assemble_rings,
    fetch_osm_elements,
    osm_forest_features,
    parse_bbox,
    parse_overpass_urls,
)


class OsmForestMaskTest(unittest.TestCase):
    def test_parse_bbox_requires_ordered_coordinates(self):
        self.assertEqual(parse_bbox("30,40,31,41"), (30.0, 40.0, 31.0, 41.0))

        with self.assertRaisesRegex(ValueError, "longitude"):
            parse_bbox("31,40,30,41")

    def test_closed_way_becomes_polygon_feature(self):
        features = osm_forest_features(
            [
                {
                    "type": "way",
                    "id": 11,
                    "tags": {"natural": "wood"},
                    "geometry": [
                        {"lon": 30.0, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.0},
                        {"lon": 30.1, "lat": 40.1},
                        {"lon": 30.0, "lat": 40.0},
                    ],
                }
            ]
        )

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["source_id"], "osm:way:11")
        self.assertEqual(features[0]["forest_type"], "wood")

    def test_relation_segments_are_assembled_into_ring(self):
        rings = _assemble_rings(
            [
                [[0, 0], [1, 0]],
                [[1, 0], [1, 1]],
                [[0, 1], [0, 0]],
                [[1, 1], [0, 1]],
            ]
        )

        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])

    def test_parse_overpass_urls_supports_fallback_list(self):
        self.assertEqual(parse_overpass_urls(" https://a.test , https://b.test "), ["https://a.test", "https://b.test"])

    def test_fetch_osm_elements_tries_next_endpoint_after_http_error(self):
        failed = Mock()
        failed.raise_for_status.side_effect = requests.HTTPError("timeout")
        ok = Mock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"elements": [{"type": "way", "id": 1}]}

        with patch("app.commands.ingest_osm_forest_mask.requests.post", side_effect=[failed, ok]) as post:
            elements = fetch_osm_elements(["https://a.test", "https://b.test"], (30, 40, 31, 41), 20)

        self.assertEqual(elements, [{"type": "way", "id": 1}])
        self.assertEqual(post.call_count, 2)

    def test_fetch_osm_elements_retries_without_relations_after_endpoint_failures(self):
        failed_a = Mock()
        failed_a.raise_for_status.side_effect = requests.HTTPError("timeout")
        failed_b = Mock()
        failed_b.raise_for_status.side_effect = requests.HTTPError("timeout")
        ok = Mock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"elements": [{"type": "way", "id": 2}]}

        with patch("app.commands.ingest_osm_forest_mask.requests.post", side_effect=[failed_a, failed_b, ok]) as post:
            elements = fetch_osm_elements(["https://a.test", "https://b.test"], (30, 40, 31, 41), 20)

        self.assertEqual(elements, [{"type": "way", "id": 2}])
        self.assertEqual(post.call_count, 3)
        fallback_query = post.call_args.kwargs["data"]["data"]
        self.assertNotIn('relation["natural"="wood"]', fallback_query)
        self.assertIn('way["natural"="wood"]', fallback_query)


if __name__ == "__main__":
    unittest.main()
