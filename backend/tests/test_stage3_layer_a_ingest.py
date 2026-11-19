import unittest
import time
from unittest.mock import patch

from app.commands.run_stage3_layer_a_ingest import (
    _aggregate_source_status,
    _aggregate_completed_run_rows,
    _bbox_from_payload,
    _fetch_bbox_with_subdivision,
    _failure_total,
    _final_run_status,
    _fetch_with_retries,
    _metadata_from_payload,
    _parse_bbox,
    _parse_source_layers,
    _retry_or_missing_tiles,
    _split_bbox,
    _source_deadline,
    _source_time_budget,
    _should_subdivide_source_error,
    _should_track_seen_features,
    _tile_count,
    _tile_error_text,
    _tile_work_items,
    _update_ingest_run,
    main,
)


class Stage3LayerAIngestTest(unittest.TestCase):
    def test_tile_count_respects_max_tiles(self):
        bbox = _parse_bbox("30,40,30.3,40.3")

        self.assertEqual(_tile_count(bbox, tile_size=0.1, max_tiles=0), 9)
        self.assertEqual(_tile_count(bbox, tile_size=0.1, max_tiles=4), 4)

    def test_final_status_fails_when_any_source_tile_failed(self):
        self.assertEqual(_final_run_status({"bolme": 0, "mescere": 0}), "completed")
        self.assertEqual(_final_run_status({"bolme": 1, "mescere": 0}), "failed")

    def test_failure_total_counts_persisted_retry_failures(self):
        self.assertEqual(_failure_total({"failed_bolme": 2, "failed_mescere": 3}), 5)
        self.assertEqual(_failure_total({"failed_bolme": None, "failed_mescere": 1}), 1)

    def test_source_status_is_aggregate_not_last_tile(self):
        self.assertEqual(_aggregate_source_status(feature_count=3, failure_count=0), "ok")
        self.assertEqual(_aggregate_source_status(feature_count=0, failure_count=0), "empty")
        self.assertEqual(_aggregate_source_status(feature_count=3, failure_count=1), "unavailable")

    def test_tile_error_text_is_short_and_source_labeled(self):
        text = _tile_error_text({"bolme": "timeout", "mescere": ""})

        self.assertEqual(text, "bolme: timeout")

    def test_update_ingest_run_clears_finished_at_when_resumed(self):
        class Db:
            statement = ""
            params = {}

            def execute(self, statement, params):
                self.statement = str(statement)
                self.params = params

        db = Db()

        _update_ingest_run(
            db,
            7,
            1,
            {"ogm_esatis_bolme": 2, "ogm_yayin_mescere_wms_svg": 0},
            {"ogm_esatis_bolme": 2, "ogm_yayin_mescere_wms_svg": 0},
            {"ogm_esatis_bolme": 0, "ogm_yayin_mescere_wms_svg": 0},
            status="running",
        )

        self.assertIn("WHEN :status = 'running' THEN NULL", db.statement)
        self.assertEqual(db.params["status"], "running")

    def test_source_time_budget_defaults_above_request_timeout(self):
        class Args:
            timeout = 5
            source_time_budget = 0

        self.assertEqual(_source_time_budget(Args()), 20)

    def test_source_time_budget_can_be_set_explicitly(self):
        class Args:
            timeout = 45
            source_time_budget = 7

        self.assertEqual(_source_time_budget(Args()), 7)

    def test_source_deadline_interrupts_hanging_source(self):
        with self.assertRaisesRegex(TimeoutError, "ogm_test"):
            with _source_deadline(1, "ogm_test"):
                time.sleep(2)

    def test_fetch_with_retries_recovers_after_transient_error(self):
        calls = {"count": 0}

        def flaky_fetch():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary upstream read error")
            return ["feature"]

        result = _fetch_with_retries("ogm_test", flaky_fetch, retries=2, retry_delay=0)

        self.assertTrue(result.ok)
        self.assertEqual(result.features, ["feature"])
        self.assertEqual(calls["count"], 2)

    def test_fetch_with_retries_reports_last_error(self):
        result = _fetch_with_retries(
            "ogm_test",
            lambda: (_ for _ in ()).throw(RuntimeError("still broken")),
            retries=2,
            retry_delay=0,
        )

        self.assertFalse(result.ok)
        self.assertIn("still broken", result.error)

    def test_split_bbox_returns_four_quadrants(self):
        self.assertEqual(
            _split_bbox((0.0, 10.0, 2.0, 12.0)),
            [
                (0.0, 10.0, 1.0, 11.0),
                (1.0, 10.0, 2.0, 11.0),
                (0.0, 11.0, 1.0, 12.0),
                (1.0, 11.0, 2.0, 12.0),
            ],
        )

    def test_fetch_bbox_with_subdivision_recovers_large_tile_failure(self):
        def fetch_bbox(bbox):
            if bbox == (0.0, 0.0, 2.0, 2.0):
                raise RuntimeError("response too large")
            return [bbox]

        result = _fetch_bbox_with_subdivision(
            source_layer="ogm_test",
            fetch_bbox=fetch_bbox,
            bbox=(0.0, 0.0, 2.0, 2.0),
            retries=1,
            retry_delay=0,
            remaining_depth=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.features), 4)

    def test_fetch_bbox_with_pre_subdivision_skips_parent_tile(self):
        calls = []

        def fetch_bbox(bbox):
            calls.append(bbox)
            if bbox == (0.0, 0.0, 2.0, 2.0):
                raise AssertionError("parent bbox should not be fetched")
            return [bbox]

        result = _fetch_bbox_with_subdivision(
            source_layer="ogm_test",
            fetch_bbox=fetch_bbox,
            bbox=(0.0, 0.0, 2.0, 2.0),
            retries=1,
            retry_delay=0,
            remaining_depth=0,
            pre_subdivide_depth=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.features), 4)
        self.assertNotIn((0.0, 0.0, 2.0, 2.0), calls)

    def test_fetch_bbox_with_subdivision_does_not_split_network_errors(self):
        calls = {"count": 0}

        def fetch_bbox(_bbox):
            calls["count"] += 1
            raise RuntimeError("ConnectionError: HTTPSConnectionPool max retries exceeded")

        result = _fetch_bbox_with_subdivision(
            source_layer="ogm_test",
            fetch_bbox=fetch_bbox,
            bbox=(0.0, 0.0, 2.0, 2.0),
            retries=1,
            retry_delay=0,
            remaining_depth=2,
        )

        self.assertFalse(result.ok)
        self.assertEqual(calls["count"], 1)

    def test_source_error_subdivision_policy(self):
        self.assertTrue(_should_subdivide_source_error("response too large; split the bbox"))
        self.assertFalse(_should_subdivide_source_error("ConnectionError: HTTPSConnectionPool max retries exceeded"))

    def test_bbox_from_payload_accepts_json_string_or_mapping(self):
        bbox = {"min_lon": 25, "min_lat": 35.5, "max_lon": 45.2, "max_lat": 42.5}

        self.assertEqual(_bbox_from_payload(bbox), (25.0, 35.5, 45.2, 42.5))
        self.assertEqual(
            _bbox_from_payload('{"min_lon":25,"min_lat":35.5,"max_lon":45.2,"max_lat":42.5}'),
            (25.0, 35.5, 45.2, 42.5),
        )

    def test_retry_or_missing_tiles_resumes_unprocessed_tail(self):
        class Result:
            def mappings(self):
                return self

            def all(self):
                return [
                    {
                        "tile_key": "1",
                        "min_lon": 0,
                        "min_lat": 0,
                        "max_lon": 0.1,
                        "max_lat": 0.1,
                        "status": "completed",
                        "failed_bolme": 0,
                        "failed_mescere": 0,
                    },
                    {
                        "tile_key": "2",
                        "min_lon": 0,
                        "min_lat": 0.1,
                        "max_lon": 0.1,
                        "max_lat": 0.2,
                        "status": "failed",
                        "failed_bolme": 1,
                        "failed_mescere": 0,
                    },
                ]

        class Db:
            def execute(self, *_args, **_kwargs):
                return Result()

        class Args:
            retry_run_id = 42
            region_code = "TR"
            tile_size = 0.1

        tiles = _retry_or_missing_tiles(
            Db(),
            {
                "bbox": {"min_lon": 0, "min_lat": 0, "max_lon": 0.2, "max_lat": 0.2},
                "tile_size": 0.1,
                "total_tiles": 4,
            },
            Args(),
        )

        self.assertEqual([tile["tile_key"] for tile in tiles], ["2", "3", "4"])
        self.assertEqual((tiles[1]["min_lon"], tiles[1]["min_lat"]), (0.1, 0.0))

    def test_metadata_from_payload_accepts_empty_json_string_or_mapping(self):
        self.assertEqual(_metadata_from_payload(None), {})
        self.assertEqual(_metadata_from_payload('{"tile_intersect_source_layers":["osm_forest_mask"]}'), {
            "tile_intersect_source_layers": ["osm_forest_mask"]
        })
        self.assertEqual(_metadata_from_payload({"a": 1}), {"a": 1})

    def test_parse_source_layers_trims_empty_values(self):
        self.assertEqual(_parse_source_layers("osm_forest_mask, ,esa_worldcover_nonforest_exclusion"), [
            "osm_forest_mask",
            "esa_worldcover_nonforest_exclusion",
        ])

    def test_tile_work_items_can_filter_by_source_layer_intersection(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def scalar_one(self):
                return self.value

        class Db:
            def execute(self, _statement, params):
                return Result(params["min_lon"] >= 0.1)

        tiles = _tile_work_items(
            Db(),
            bbox=(0.0, 0.0, 0.2, 0.2),
            tile_size=0.1,
            max_tiles=0,
            region_code="TR",
            intersect_source_layers=["osm_forest_mask"],
        )

        self.assertEqual([tile["tile_key"] for tile in tiles], [1, 2])
        self.assertEqual(tiles[0]["bbox"], (0.1, 0.0, 0.2, 0.1))
        self.assertEqual(tiles[1]["bbox"], (0.1, 0.1, 0.2, 0.2))

    def test_tile_work_items_honors_max_tiles_after_filtering(self):
        tiles = _tile_work_items(
            db=None,
            bbox=(0.0, 0.0, 0.2, 0.2),
            tile_size=0.1,
            max_tiles=2,
            region_code="TR",
            intersect_source_layers=[],
        )

        self.assertEqual([tile["tile_key"] for tile in tiles], [1, 2])

    def test_tile_work_items_splits_filtered_tiles_into_stable_shards(self):
        shard_zero = _tile_work_items(
            db=None,
            bbox=(0.0, 0.0, 0.2, 0.2),
            tile_size=0.1,
            max_tiles=0,
            region_code="TR",
            intersect_source_layers=[],
            tile_shard_count=2,
            tile_shard_index=0,
        )
        shard_one = _tile_work_items(
            db=None,
            bbox=(0.0, 0.0, 0.2, 0.2),
            tile_size=0.1,
            max_tiles=0,
            region_code="TR",
            intersect_source_layers=[],
            tile_shard_count=2,
            tile_shard_index=1,
        )

        self.assertEqual([tile["tile_key"] for tile in shard_zero], [1, 3])
        self.assertEqual([tile["tile_key"] for tile in shard_one], [2, 4])

    def test_completed_run_aggregation_requires_full_shard_set(self):
        rows = [
            {
                "id": 12,
                "source": "bolme",
                "status": "completed",
                "tile_size": 0.12,
                "total_tiles": 2,
                "processed_tiles": 2,
                "bolme_fetched": 5,
                "mescere_fetched": 0,
                "bolme_inserted": 5,
                "mescere_inserted": 0,
                "failed_bolme": 0,
                "failed_mescere": 0,
                "bbox": {"min_lon": 25, "min_lat": 35, "max_lon": 26, "max_lat": 36},
                "metadata": {
                    "tile_shard_count": 2,
                    "tile_shard_index": 0,
                    "tile_intersect_source_layers": ["osm_forest_mask"],
                    "bbox_source": "region",
                    "region_geometry_hash": "abc",
                },
            },
            {
                "id": 13,
                "source": "bolme",
                "status": "completed",
                "tile_size": 0.12,
                "total_tiles": 2,
                "processed_tiles": 2,
                "bolme_fetched": 7,
                "mescere_fetched": 0,
                "bolme_inserted": 7,
                "mescere_inserted": 0,
                "failed_bolme": 0,
                "failed_mescere": 0,
                "bbox": {"min_lon": 25, "min_lat": 35, "max_lon": 26, "max_lat": 36},
                "metadata": {
                    "tile_shard_count": 2,
                    "tile_shard_index": 1,
                    "tile_intersect_source_layers": ["osm_forest_mask"],
                    "bbox_source": "region",
                    "region_geometry_hash": "abc",
                },
            },
        ]

        aggregate = _aggregate_completed_run_rows(rows)

        self.assertEqual(aggregate["source"], "bolme")
        self.assertEqual(aggregate["run_ids"], [12, 13])
        self.assertEqual(aggregate["total_tiles"], 4)
        self.assertEqual(aggregate["fetched"]["ogm_esatis_bolme"], 12)

    def test_completed_run_aggregation_rejects_missing_shard(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            _aggregate_completed_run_rows(
                [
                    {
                        "id": 12,
                        "source": "bolme",
                        "status": "completed",
                        "tile_size": 0.12,
                        "total_tiles": 2,
                        "processed_tiles": 2,
                        "bolme_fetched": 5,
                        "mescere_fetched": 0,
                        "bolme_inserted": 5,
                        "mescere_inserted": 0,
                        "failed_bolme": 0,
                        "failed_mescere": 0,
                        "bbox": {"min_lon": 25, "min_lat": 35, "max_lon": 26, "max_lat": 36},
                        "metadata": {"tile_shard_count": 2, "tile_shard_index": 0, "bbox_source": "region"},
                    }
                ]
            )

    def test_retry_or_missing_tiles_uses_original_shard_metadata(self):
        class Result:
            def mappings(self):
                return self

            def all(self):
                return [
                    {
                        "tile_key": "1",
                        "min_lon": 0,
                        "min_lat": 0,
                        "max_lon": 0.1,
                        "max_lat": 0.1,
                        "status": "completed",
                        "failed_bolme": 0,
                        "failed_mescere": 0,
                    },
                    {
                        "tile_key": "3",
                        "min_lon": 0.1,
                        "min_lat": 0,
                        "max_lon": 0.2,
                        "max_lat": 0.1,
                        "status": "failed",
                        "failed_bolme": 1,
                        "failed_mescere": 0,
                    },
                ]

        class Db:
            def execute(self, *_args, **_kwargs):
                return Result()

        class Args:
            retry_run_id = 42
            region_code = "TR"
            tile_size = 0.1

        tiles = _retry_or_missing_tiles(
            Db(),
            {
                "bbox": {"min_lon": 0, "min_lat": 0, "max_lon": 0.2, "max_lat": 0.2},
                "tile_size": 0.1,
                "total_tiles": 2,
                "metadata": {"tile_shard_count": 2, "tile_shard_index": 0},
            },
            Args(),
        )

        self.assertEqual([tile["tile_key"] for tile in tiles], ["3"])

    def test_filtered_tile_run_cannot_retire_missing_features(self):
        argv = [
            "run_stage3_layer_a_ingest",
            "--region-code",
            "TR",
            "--bbox-from-region",
            "--tile-intersect-source-layers",
            "osm_forest_mask",
            "--retire-missing-bolme",
        ]

        with patch("sys.argv", argv), self.assertRaisesRegex(ValueError, "retire_missing"):
            main()

    def test_filtered_tile_run_allows_explicit_no_retire(self):
        argv = [
            "run_stage3_layer_a_ingest",
            "--region-code",
            "TR",
            "--bbox-from-region",
            "--tile-intersect-source-layers",
            "osm_forest_mask",
            "--retire-missing-bolme",
            "--no-retire-missing-bolme",
            "--plan-only",
        ]

        with (
            patch("sys.argv", argv),
            patch("app.commands.run_stage3_layer_a_ingest.get_settings", side_effect=RuntimeError("past validation")),
            self.assertRaisesRegex(RuntimeError, "past validation"),
        ):
            main()

    def test_filtered_no_retire_run_does_not_need_seen_table(self):
        class Args:
            source = "bolme"
            retire_missing_bolme = True
            no_retire_missing_bolme = True
            retire_missing_mescere = False
            no_retire_missing_mescere = False

        self.assertFalse(_should_track_seen_features(Args()))

    def test_unfiltered_retire_run_tracks_seen_features(self):
        class Args:
            source = "all"
            retire_missing_bolme = True
            no_retire_missing_bolme = False
            retire_missing_mescere = False
            no_retire_missing_mescere = False

        self.assertTrue(_should_track_seen_features(Args()))

    def test_tile_delay_must_not_be_negative(self):
        argv = [
            "run_stage3_layer_a_ingest",
            "--region-code",
            "TR",
            "--bbox",
            "1,2,3,4",
            "--tile-delay",
            "-1",
        ]

        with patch("sys.argv", argv), self.assertRaisesRegex(ValueError, "tile_delay"):
            main()


if __name__ == "__main__":
    unittest.main()
