import unittest
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np

from app.services.stage4_public_cog import (
    _apply_scale_offset,
    _rank_public_stac_scenes,
    _read_asset_with_retry,
    _write_ndvi_mosaic,
)


class Stage4PublicCogRadiometryTest(unittest.TestCase):
    def test_zero_cloud_cover_precedes_cloudy_and_missing_values(self):
        scenes = [
            {"id": name, "bbox": [0, 0, 2, 2], "properties": {"eo:cloud_cover": cloud}}
            for name, cloud in [("cloudy", 10), ("unknown", None), ("clear", 0)]
        ]
        scenes.append({"id": "missing", "bbox": [0, 0, 2, 2]})
        result = _rank_public_stac_scenes(scenes, [0, 0, 2, 2])
        self.assertEqual([scene["id"] for scene in result], ["clear", "cloudy", "unknown", "missing"])

    def test_scene_coverage_still_takes_precedence_over_cloud_rank(self):
        scenes = [
            {"id": "clear-partial", "bbox": [0, 0, 1, 1], "properties": {"eo:cloud_cover": 0}},
            {"id": "cloudy-full", "bbox": [0, 0, 2, 2], "properties": {"eo:cloud_cover": 10}},
        ]
        result = _rank_public_stac_scenes(scenes, [0, 0, 2, 2])
        self.assertEqual([scene["id"] for scene in result], ["cloudy-full", "clear-partial"])

    @staticmethod
    def scene(collection, flag=True):
        return {
            "collection": collection,
            "properties": {"earthsearch:boa_offset_applied": flag},
            "assets": {
                key: {"raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
                for key in ("red", "nir")
            },
        }

    def test_paired_legacy_and_collection_one_arrays_have_equal_reflectance_and_ndvi(self):
        # Includes the actual paired probe's red=274, nir=2411 DN sample.
        # Collection 1 stores the corresponding 1000-DN offset in its pixels.
        red = np.array([[274, 400, 600], [800, 1000, 1800]], dtype="uint16")
        nir = np.array([[2411, 2800, 3000], [3200, 5000, 6000]], dtype="uint16")
        legacy = self.scene("sentinel-2-l2a")
        collection_one = self.scene("sentinel-2-c1-l2a")

        legacy_red = _apply_scale_offset(red, legacy, "red")
        legacy_nir = _apply_scale_offset(nir, legacy, "nir")
        c1_red = _apply_scale_offset(red + 1000, collection_one, "red")
        c1_nir = _apply_scale_offset(nir + 1000, collection_one, "nir")

        np.testing.assert_allclose(legacy_red, red.astype("float64") * 0.0001, rtol=0, atol=5e-8)
        np.testing.assert_allclose(legacy_nir, nir.astype("float64") * 0.0001, rtol=0, atol=5e-8)
        np.testing.assert_allclose(legacy_red, c1_red, rtol=0, atol=5e-8)
        np.testing.assert_allclose(legacy_nir, c1_nir, rtol=0, atol=5e-8)
        legacy_ndvi = (legacy_nir - legacy_red) / (legacy_nir + legacy_red)
        c1_ndvi = (c1_nir - c1_red) / (c1_nir + c1_red)
        expected_ndvi = (nir.astype("float64") - red) / (nir.astype("float64") + red)
        np.testing.assert_allclose(legacy_ndvi, expected_ndvi, rtol=0, atol=5e-7)
        np.testing.assert_allclose(legacy_ndvi, c1_ndvi, rtol=0, atol=5e-7)
        self.assertTrue(np.all(legacy_red > 0))
        self.assertTrue(np.all(legacy_ndvi < 1))

    def test_legacy_false_flag_applies_declared_offset(self):
        scene = self.scene("sentinel-2-l2a", False)
        raw = np.array([1274, 2411], dtype="uint16")

        for key in ("red", "nir"):
            with self.subTest(asset=key):
                np.testing.assert_allclose(
                    _apply_scale_offset(raw, scene, key), [0.0274, 0.1411], rtol=0, atol=5e-8
                )

    def test_missing_flag_uses_metadata_even_at_a_recent_processing_baseline(self):
        scene = self.scene("sentinel-2-l2a")
        scene["properties"] = {"s2:processing_baseline": "05.13"}

        np.testing.assert_allclose(
            _apply_scale_offset(np.array([1274], dtype="uint16"), scene, "red"),
            [0.0274], rtol=0, atol=5e-8,
        )

    def test_only_boolean_true_overrides_the_legacy_offset(self):
        for flag in (None, "true", "false", 1, 0):
            with self.subTest(flag=flag):
                scene = self.scene("sentinel-2-l2a", flag)
                np.testing.assert_allclose(
                    _apply_scale_offset(np.array([1274], dtype="uint16"), scene, "red"),
                    [0.0274], rtol=0, atol=5e-8,
                )

    def test_collection_one_always_applies_metadata_despite_offset_applied_flag(self):
        for flag in (True, False, None):
            with self.subTest(flag=flag):
                scene = self.scene("sentinel-2-c1-l2a", flag)
                np.testing.assert_allclose(
                    _apply_scale_offset(np.array([1274, 3411], dtype="uint16"), scene, "nir"),
                    [0.0274, 0.2411], rtol=0, atol=5e-8,
                )

    def test_other_and_missing_collections_preserve_declared_radiometry(self):
        for collection in ("other-provider-l2a", "sentinel-2-l1c", None):
            with self.subTest(collection=collection):
                scene = self.scene(collection)
                if collection is None:
                    scene.pop("collection")
                scene["assets"]["red"]["raster:bands"] = [{"scale": 0.0002, "offset": -0.05}]
                np.testing.assert_allclose(
                    _apply_scale_offset(np.array([1274], dtype="uint16"), scene, "red"),
                    [0.2048], rtol=0, atol=5e-8,
                )

    def test_scl_classes_are_unchanged_if_scale_helper_is_called(self):
        classes = np.array([[0, 1, 4], [5, 6, 11]], dtype="uint8")
        for collection in ("sentinel-2-l2a", "sentinel-2-c1-l2a", "other-provider"):
            with self.subTest(collection=collection):
                scene = self.scene(collection)
                scene["assets"]["scl"] = {"raster:bands": [{"nodata": 0, "data_type": "uint8"}]}
                np.testing.assert_array_equal(_apply_scale_offset(classes, scene, "scl"), classes)

    def test_legacy_override_does_not_change_other_asset_offsets(self):
        scene = self.scene("sentinel-2-l2a")
        scene["assets"]["other"] = {"raster:bands": [{"scale": 1, "offset": 2}]}

        np.testing.assert_array_equal(
            _apply_scale_offset(np.array([4, 5], dtype="uint8"), scene, "other"), [6, 7]
        )

    def test_missing_band_metadata_keeps_existing_defaults_and_zero_values(self):
        scene = self.scene("sentinel-2-l2a")
        scene["assets"]["red"] = {}

        np.testing.assert_array_equal(
            _apply_scale_offset(np.array([0, 274], dtype="uint16"), scene, "red"), [0, 274]
        )


class Stage4PublicCogReadFailureTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("rasterio"), "rasterio required")
    def test_cell_values_do_not_change_when_other_cells_need_more_scenes(self):
        import rasterio

        scenes = [{"id":name, "assets":{key:{} for key in ("red","nir","scl")}}
                  for name in ("primary","secondary")]
        def read(scene, asset, *args):
            if asset == "scl":
                values = [4,9,9,9] if scene["id"] == "primary" else [4,4,4,4]
            else:
                values = [2]*4 if asset == "red" else ([6]*4 if scene["id"] == "primary" else [18]*4)
            return {"array":np.array([values],dtype="float32"), "valid":np.ones((1,4),dtype=bool)}
        with TemporaryDirectory() as directory, patch("app.services.stage4_public_cog._read_asset",side_effect=read):
            values = []
            for name,zones,counts,required in (("small",[1,1,0,0],[0,2],[0,1]),
                                               ("expanded",[1,1,2,2],[0,2,2],[0,1,1])):
                output = Path(directory) / (name+".tif")
                stats = _write_ndvi_mosaic(output,scenes=scenes,bbox=[0,0,40,10],crs_epsg=6933,
                    width=4,height=1,http_timeout_seconds=1,target_mask={
                        "zones":np.array([zones]),"sample_counts":counts,"required_counts":required,
                        "min_valid_pixel_ratio":0.5})
                with rasterio.open(output) as raster:
                    values.append(raster.read()[:,:,:2])
                self.assertEqual(stats["scenes_attempted"],1 if name=="small" else 2)
                self.assertTrue(stats["target_coverage_complete"])
            np.testing.assert_array_equal(values[0],values[1])
            np.testing.assert_array_equal(values[0][1],[[1,0]])

    def test_invalid_asset_metadata_is_not_reported_as_temporary_outage(self):
        with patch("app.services.stage4_public_cog._read_asset", side_effect=KeyError("red")) as read:
            with self.assertRaises(KeyError):
                _read_asset_with_retry("primary", "red")
        self.assertEqual(read.call_count, 1)

    def test_transient_read_retries_same_asset_with_heartbeat(self):
        heartbeat = Mock()
        expected = object()
        with patch("app.services.stage4_public_cog._read_asset", side_effect=[OSError("temporary"), expected]) as read, \
             patch("app.services.stage4_public_cog.time.sleep") as sleep:
            self.assertIs(_read_asset_with_retry("primary", "red", heartbeat=heartbeat), expected)
        self.assertEqual(read.call_args_list[0], read.call_args_list[1])
        self.assertEqual(read.call_count, 2)
        heartbeat.assert_called_once()
        sleep.assert_called_once_with(1)

    @unittest.skipUnless(importlib.util.find_spec("rasterio"), "rasterio required")
    def test_unreadable_primary_never_substitutes_secondary_or_writes_raster(self):
        with TemporaryDirectory() as directory, \
             patch("app.services.stage4_public_cog._read_asset", side_effect=OSError("temporary")) as read, \
             patch("app.services.stage4_public_cog.time.sleep"):
            output = Path(directory) / "ndvi.tif"
            with self.assertRaisesRegex(RuntimeError, "retry the batch"):
                _write_ndvi_mosaic(output, scenes=[{"id":"primary"}, {"id":"secondary"}],
                    bbox=[0,0,10,10], crs_epsg=6933, width=1, height=1, http_timeout_seconds=1)
            self.assertEqual(read.call_count, 3)
            self.assertTrue(all(call.args[0]["id"] == "primary" for call in read.call_args_list))
            self.assertFalse(output.exists())

    @unittest.skipUnless(importlib.util.find_spec("rasterio"), "rasterio required")
    def test_cloudy_primary_can_still_use_clear_secondary(self):
        def read(scene, asset, *args):
            value = (9 if scene["id"] == "cloudy" else 4) if asset == "scl" else (2 if asset == "red" else 6)
            return {"array":np.array([[value]], dtype="float32"), "valid":np.array([[True]])}
        with TemporaryDirectory() as directory, patch("app.services.stage4_public_cog._read_asset", side_effect=read):
            output = Path(directory) / "ndvi.tif"
            scenes = [{"id":name, "assets":{key:{} for key in ("red","nir","scl")}} for name in ("cloudy","clear")]
            stats = _write_ndvi_mosaic(output, scenes=scenes,
                bbox=[0,0,10,10], crs_epsg=6933, width=1, height=1, http_timeout_seconds=1)
            self.assertEqual(stats["scenes_attempted"], 2)
            self.assertEqual(stats["scenes_used"], 1)
            self.assertEqual(stats["skipped_scene_count"], 0)
            self.assertTrue(stats["target_coverage_complete"])
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
