import inspect
import unittest
from unittest.mock import DEFAULT, Mock, patch

from app.commands.check_stage3_forest_clip import (
    FOREST_COVERAGE_TOLERANCE_M2,
    MASK_EDGE_TOLERANCE_M,
    SOURCE_MASK_SCALE_TOLERANCE_CAP_M2,
    _region_mask_component_tile_batch,
    _source_mask_scale_tolerance_m2,
    _source_mask_cache_tile_batch,
    _source_mask_cache_exclusion_tile_batch,
    forest_clip_report,
)


class ForestClipDb:
    def __init__(
        self,
        metadata,
        *,
        run_metadata=None,
        grid_run_status="completed",
        rebuilt_mask_difference_ratio=0,
        nonforest_overlap_cells=0,
        total_nonforest_overlap_m2=None,
        uncovered_forest_area_m2=0,
        total_outside_forest_mask_m2=0,
        pending_tiles=0,
    ):
        self.metadata = metadata
        self.run_metadata = run_metadata or {}
        self.grid_run_status = grid_run_status
        self.rebuilt_mask_difference_ratio = rebuilt_mask_difference_ratio
        self.nonforest_overlap_cells = nonforest_overlap_cells
        if total_nonforest_overlap_m2 is None:
            total_nonforest_overlap_m2 = 12 if nonforest_overlap_cells else 0
        self.total_nonforest_overlap_m2 = total_nonforest_overlap_m2
        self.uncovered_forest_area_m2 = uncovered_forest_area_m2
        self.total_outside_forest_mask_m2 = total_outside_forest_mask_m2
        self.pending_tiles = pending_tiles
        self.sql = ""

    def execute(self, *_args, **_kwargs):
        self.sql = str(_args[0])
        if "latest_grid_run AS" in self.sql:
            return ForestClipContextResult(self.metadata, self.run_metadata, self.grid_run_status)
        if "tile_differences AS" in self.sql:
            return SourceMaskForestClipResult(
                self.metadata,
                self.grid_run_status,
                self.rebuilt_mask_difference_ratio,
                self.nonforest_overlap_cells,
                self.total_nonforest_overlap_m2,
                self.uncovered_forest_area_m2,
                self.total_outside_forest_mask_m2,
                self.pending_tiles,
            )
        return ForestClipResult(
            self.metadata,
            self.rebuilt_mask_difference_ratio,
            self.nonforest_overlap_cells,
            self.total_nonforest_overlap_m2,
            self.uncovered_forest_area_m2,
        )


class ForestClipContextResult:
    def __init__(self, metadata, run_metadata, grid_run_status):
        self.metadata = metadata
        self.run_metadata = run_metadata
        self.grid_run_status = grid_run_status

    def mappings(self):
        return self

    def one(self):
        return {
            "region_id": 1,
            "region_metadata": self.metadata,
            "run_metadata": self.run_metadata,
            "grid_run_status": self.grid_run_status,
            "total_tiles": 10,
            "processed_tiles": 10,
        }


class ForestClipResult:
    def __init__(
        self,
        metadata,
        rebuilt_mask_difference_ratio,
        nonforest_overlap_cells,
        total_nonforest_overlap_m2,
        uncovered_forest_area_m2,
    ):
        self.metadata = metadata
        self.rebuilt_mask_difference_ratio = rebuilt_mask_difference_ratio
        self.nonforest_overlap_cells = nonforest_overlap_cells
        self.total_nonforest_overlap_m2 = total_nonforest_overlap_m2
        self.uncovered_forest_area_m2 = uncovered_forest_area_m2

    def mappings(self):
        return self

    def one(self):
        return {
            "cell_count": 10,
            "invalid_cells": 0,
            "cells_outside_forest_mask": 0,
            "total_outside_forest_mask_m2": 0,
            "cells_intersect_nonforest_exclusion": self.nonforest_overlap_cells,
            "total_nonforest_exclusion_overlap_m2": self.total_nonforest_overlap_m2,
            "oversized_cells": 0,
            "clipped_edge_cells": 2,
            "polygonal_cells": 10,
            "rebuilt_mask_available": True,
            "rebuilt_difference_mask_available": True,
            "rebuilt_mask_difference_ratio": self.rebuilt_mask_difference_ratio,
            "forest_mask_coverage_ratio": 1,
            "forest_mask_area_ha": 10,
            "active_grid_area_ha": 10,
            "uncovered_forest_area_m2": self.uncovered_forest_area_m2,
            "uncovered_forest_area_ha": self.uncovered_forest_area_m2 / 10000,
            "region_metadata": self.metadata,
        }


class SourceMaskForestClipResult(ForestClipResult):
    def __init__(
        self,
        metadata,
        grid_run_status,
        rebuilt_mask_difference_ratio,
        nonforest_overlap_cells,
        total_nonforest_overlap_m2,
        uncovered_forest_area_m2,
        total_outside_forest_mask_m2,
        pending_tiles,
    ):
        super().__init__(
            metadata,
            rebuilt_mask_difference_ratio,
            nonforest_overlap_cells,
            total_nonforest_overlap_m2,
            uncovered_forest_area_m2,
        )
        self.grid_run_status = grid_run_status
        self.total_outside_forest_mask_m2 = total_outside_forest_mask_m2
        self.pending_tiles = pending_tiles

    def one(self):
        row = super().one()
        row.update(
            {
                "cells_outside_forest_mask": 1 if self.total_outside_forest_mask_m2 else 0,
                "total_outside_forest_mask_m2": self.total_outside_forest_mask_m2,
                "completed_tiles": 10 - self.pending_tiles,
                "pending_tiles": self.pending_tiles,
                "running_tiles": 0,
                "failed_tiles": 0,
                "grid_run_status": self.grid_run_status,
            }
        )
        return row


class Stage3ForestClipTest(unittest.TestCase):
    def test_tile_level_validation_clips_cells_to_tile_before_difference(self):
        region_component_sql = inspect.getsource(_region_mask_component_tile_batch)
        source_cache_sql = inspect.getsource(_source_mask_cache_tile_batch)

        for sql in (region_component_sql, source_cache_sql):
            self.assertIn("ST_Intersection(", sql)
            self.assertIn("ST_Transform(ST_MakeValid(cell.geom), :projected_srid)", sql)
            self.assertIn("tile.geom", sql)
            self.assertIn("ST_PointOnSurface(cell.geom)", sql)
            self.assertIn("tile_seams AS", sql)
            self.assertIn("ST_Boundary(tile_geoms.geom)", sql)
            self.assertIn("ST_Boundary(tile_mask.geom)", sql)
            self.assertIn("tile_seam_tolerance_m", sql)
            self.assertIn("mask_edge_tolerance_m", sql)

    def test_source_mask_scale_tolerance_keeps_absolute_floor_for_small_scopes(self):
        self.assertEqual(_source_mask_scale_tolerance_m2(10_000), FOREST_COVERAGE_TOLERANCE_M2)

    def test_source_mask_scale_tolerance_caps_country_scale_sliver_budget(self):
        self.assertEqual(_source_mask_scale_tolerance_m2(100_000_000_000), SOURCE_MASK_SCALE_TOLERANCE_CAP_M2)

    def test_report_requires_real_osm_intersect_metadata(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_ogm_layer_a_with_osm_forest_clip",
                        "source_layers": ["ogm_esatis_bolme"],
                        "intersect_source_layers": [],
                        "source_qualities": ["public_ogm_rest"],
                        "intersect_source_qualities": [],
                        "production_roles": ["active_public_layer_a"],
                        "intersect_production_roles": [],
                    },
                }
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertFalse(report["checks"]["forest_mask_has_public_osm_clip"])
        self.assertFalse(report["checks"]["ready"])

    def test_report_accepts_public_ogm_with_osm_intersect_and_difference_metadata(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
                        "source_layers": ["ogm_esatis_bolme"],
                        "intersect_source_layers": ["osm_forest_mask"],
                        "difference_source_layers": [
                            "osm_nonforest_exclusion",
                            "esa_worldcover_nonforest_exclusion",
                        ],
                        "source_qualities": ["public_ogm_rest"],
                        "intersect_source_qualities": ["public_osm_forest"],
                        "difference_source_qualities": [
                            "public_osm_nonforest_exclusion",
                            "public_satellite_landcover_nonforest_exclusion",
                        ],
                        "production_roles": ["active_public_layer_a"],
                        "intersect_production_roles": ["auxiliary_public_forest_mask"],
                        "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                        "clean_mask_policy": {
                            "method": "component_area_filter_before_grid_generation",
                            "min_component_area_ha": 1.0,
                            "applies_before_grid_insert": True,
                        },
                    },
                }
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertTrue(report["checks"]["forest_mask_has_public_osm_clip"])
        self.assertTrue(report["checks"]["ready"])

    def test_report_accepts_public_osm_only_fallback_with_difference_metadata(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_osm_forest_mask_with_nonforest_difference",
                        "source_region_code": "TR-SEED",
                        "source_layers": ["osm_forest_mask"],
                        "difference_source_layers": [
                            "osm_nonforest_exclusion",
                            "esa_worldcover_nonforest_exclusion",
                        ],
                        "source_qualities": ["public_osm_forest"],
                        "difference_source_qualities": [
                            "public_osm_nonforest_exclusion",
                            "public_satellite_landcover_nonforest_exclusion",
                        ],
                        "production_roles": ["auxiliary_public_forest_mask"],
                        "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                        "clean_mask_policy": {
                            "method": "component_area_filter_before_grid_generation",
                            "min_component_area_ha": 1.0,
                            "applies_before_grid_insert": True,
                        },
                    },
                }
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertTrue(report["checks"]["forest_mask_has_public_osm_clip"])
        self.assertTrue(report["checks"]["forest_mask_has_public_nonforest_difference"])
        self.assertTrue(report["checks"]["ready"])

    def test_rebuild_query_uses_forest_mask_source_region(self):
        db = ForestClipDb(
            {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {
                    "source": "public_osm_forest_mask",
                    "source_region_code": "TR-SEED",
                    "source_layers": ["osm_forest_mask"],
                    "source_qualities": ["public_osm_forest"],
                    "production_roles": ["auxiliary_public_forest_mask"],
                },
            }
        )

        forest_clip_report(db, region_code="TR-FOREST", grid_version="stage3-v1")

        self.assertIn("source_scope.source_region_code", db.sql)

    def test_clip_query_uses_cell_union_for_absolute_forest_coverage(self):
        db = ForestClipDb(
            {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {
                    "source": "public_osm_forest_mask_with_nonforest_difference",
                    "source_region_code": "TR-SEED",
                    "source_layers": ["osm_forest_mask"],
                    "difference_source_layers": ["osm_nonforest_exclusion"],
                    "source_qualities": ["public_osm_forest"],
                    "difference_source_qualities": ["public_osm_nonforest_exclusion"],
                    "production_roles": ["auxiliary_public_forest_mask"],
                    "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                },
            }
        )

        forest_clip_report(db, region_code="TR-FOREST", grid_version="stage3-v1")

        self.assertIn("cell_union AS", db.sql)
        self.assertIn("ST_Difference(region.geom, cell_union.geom)", db.sql)
        self.assertIn("uncovered_forest_area_m2", db.sql)

    def test_source_mask_grid_check_uses_tile_queue_not_region_union(self):
        db = ForestClipDb(
            {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {
                    "source": "public_osm_forest_mask_with_nonforest_difference",
                    "source_region_code": "TR-SEED",
                    "source_layers": ["osm_forest_mask"],
                    "difference_source_layers": [
                        "osm_nonforest_exclusion",
                        "esa_worldcover_nonforest_exclusion",
                    ],
                    "source_qualities": ["public_osm_forest"],
                    "difference_source_qualities": [
                        "public_osm_nonforest_exclusion",
                        "public_satellite_landcover_nonforest_exclusion",
                    ],
                    "production_roles": ["auxiliary_public_forest_mask"],
                    "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                    "clean_mask_policy": {
                        "method": "component_area_filter_before_grid_generation",
                        "min_component_area_ha": 1.0,
                        "applies_before_grid_insert": True,
                    },
                },
            },
            run_metadata={
                "grid_mask_method": "per_tile_source_mask",
                "mask_source_layers": ["osm_forest_mask"],
                "exclude_source_layers": [
                    "osm_nonforest_exclusion",
                    "esa_worldcover_nonforest_exclusion",
                ],
                "mask_source_region_code": "TR-SEED",
            },
        )

        report = forest_clip_report(db, region_code="TR-FOREST", grid_version="stage3-v1")

        self.assertIn("stage3_grid_generation_tiles", db.sql)
        self.assertIn("tile_differences AS", db.sql)
        self.assertIn("ST_Difference(tile_mask.geom", db.sql)
        self.assertTrue(report["checks"]["all_source_mask_tiles_completed"])
        self.assertTrue(report["checks"]["ready"])

    def test_source_mask_grid_check_rejects_incomplete_tile_queue(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_osm_forest_mask_with_nonforest_difference",
                        "source_region_code": "TR-SEED",
                        "source_layers": ["osm_forest_mask"],
                        "difference_source_layers": [
                            "osm_nonforest_exclusion",
                            "esa_worldcover_nonforest_exclusion",
                        ],
                        "source_qualities": ["public_osm_forest"],
                        "difference_source_qualities": [
                            "public_osm_nonforest_exclusion",
                            "public_satellite_landcover_nonforest_exclusion",
                        ],
                        "production_roles": ["auxiliary_public_forest_mask"],
                        "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                    },
                },
                run_metadata={
                    "grid_mask_method": "per_tile_source_mask",
                    "mask_source_layers": ["osm_forest_mask"],
                    "exclude_source_layers": [
                        "osm_nonforest_exclusion",
                        "esa_worldcover_nonforest_exclusion",
                    ],
                    "mask_source_region_code": "TR-SEED",
                },
                grid_run_status="running",
                pending_tiles=2,
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertFalse(report["checks"]["all_source_mask_tiles_completed"])
        self.assertFalse(report["checks"]["ready"])

    def test_report_rejects_uncovered_forest_mask_area(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_osm_forest_mask_with_nonforest_difference",
                        "source_region_code": "TR-SEED",
                        "source_layers": ["osm_forest_mask"],
                        "difference_source_layers": ["osm_nonforest_exclusion"],
                        "source_qualities": ["public_osm_forest"],
                        "difference_source_qualities": ["public_osm_nonforest_exclusion"],
                        "production_roles": ["auxiliary_public_forest_mask"],
                        "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                    },
                },
                uncovered_forest_area_m2=50,
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertFalse(report["checks"]["forest_mask_covered_by_clipped_grid"])
        self.assertFalse(report["checks"]["ready"])

    def test_report_rejects_region_that_does_not_match_rebuilt_mask(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_ogm_layer_a_with_osm_forest_clip",
                        "source_layers": ["ogm_esatis_bolme"],
                        "intersect_source_layers": ["osm_forest_mask"],
                        "difference_source_layers": [],
                        "source_qualities": ["public_ogm_rest"],
                        "intersect_source_qualities": ["public_osm_forest"],
                        "difference_source_qualities": [],
                        "production_roles": ["active_public_layer_a"],
                        "intersect_production_roles": ["auxiliary_public_forest_mask"],
                        "difference_production_roles": [],
                    },
                },
                rebuilt_mask_difference_ratio=0.2,
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertFalse(report["checks"]["region_matches_rebuilt_ogm_osm_mask"])
        self.assertFalse(report["checks"]["ready"])

    def test_report_rejects_cells_that_intersect_nonforest_exclusion(self):
        report = forest_clip_report(
            ForestClipDb(
                {
                    "stage3_region_source": "forest_mask_layer_a",
                    "forest_only": True,
                    "forest_mask": {
                        "source": "public_osm_forest_mask_with_nonforest_difference",
                        "source_region_code": "TR-SEED",
                        "source_layers": ["osm_forest_mask"],
                        "difference_source_layers": ["osm_nonforest_exclusion"],
                        "source_qualities": ["public_osm_forest"],
                        "difference_source_qualities": ["public_osm_nonforest_exclusion"],
                        "production_roles": ["auxiliary_public_forest_mask"],
                        "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                    },
                },
                nonforest_overlap_cells=1,
            ),
            region_code="TR-FOREST",
            grid_version="stage3-v1",
        )

        self.assertEqual(report["cells_intersect_nonforest_exclusion"], 1)
        self.assertFalse(report["checks"]["no_cells_intersect_nonforest_exclusion"])
        self.assertFalse(report["checks"]["ready"])


class Stage3RegionMaskComponentsForestClipTest(unittest.TestCase):
    def setUp(self):
        patches = patch.multiple(
            "app.commands.check_stage3_forest_clip",
            _load_clip_context=DEFAULT,
            _source_mask_cache_grid_run=DEFAULT,
            _source_mask_cache_cell_metrics=DEFAULT,
            _region_mask_component_summary=DEFAULT,
            _source_mask_cache_counts=DEFAULT,
            _region_mask_component_tile_batch=DEFAULT,
            _source_mask_cache_exclusion_tile_batch=DEFAULT,
        )
        self.helpers = patches.start()
        self.addCleanup(patches.stop)
        self.metadata = {
            "stage3_region_source": "forest_mask_layer_a",
            "forest_only": True,
            "forest_mask": {
                "source": "public_osm_forest_mask_with_nonforest_difference",
                "source_region_code": "TR-SOURCE",
                "mask_version": "small-mask",
                "source_layers": ["osm_forest_mask"],
                "difference_source_layers": ["osm_nonforest_exclusion"],
                "source_qualities": ["public_osm_forest"],
                "difference_source_qualities": ["public_osm_nonforest_exclusion"],
                "production_roles": ["auxiliary_public_forest_mask"],
                "difference_production_roles": ["auxiliary_public_nonforest_exclusion"],
                "clean_mask_policy": {
                    "method": "component_area_filter_before_grid_generation",
                    "min_component_area_ha": 1.0,
                    "applies_before_grid_insert": True,
                },
            },
        }
        self.context = {
            "region_metadata": self.metadata,
            "run_metadata": {"grid_mask_method": "region_mask_components"},
        }
        self.helpers["_load_clip_context"].return_value = self.context
        self.grid_run = {
            "run_id": 7,
            "region_id": 9,
            "projected_srid": 6933,
            "status": "completed",
            "total_tiles": 2,
            "processed_tiles": 2,
        }
        self.helpers["_source_mask_cache_grid_run"].return_value = self.grid_run
        self.helpers["_source_mask_cache_cell_metrics"].return_value = {
            "cell_count": 23553,
            "invalid_cells": 0,
            "oversized_cells": 0,
            "clipped_edge_cells": 5013,
            "polygonal_cells": 23553,
            "active_grid_area_ha": 20000,
        }
        self.helpers["_region_mask_component_summary"].return_value = {
            "component_count": 10,
            "invalid_components": 0,
            "components_below_min_area": 0,
            "min_component_area_ha": 1,
        }
        self.helpers["_source_mask_cache_counts"].return_value = {
            "osm_nonforest_exclusion": 20,
        }
        self.coverage = {
            "tile_count": 2,
            "completed_tiles": 2,
            "source_mask_tiles": 2,
            "tiles_outside_forest_mask": 0,
            "raw_tiles_outside_forest_mask": 2,
            "source_mask_area_m2": 200_000_000,
            "uncovered_forest_area_m2": 0,
            "outside_forest_mask_m2": 0,
            "raw_uncovered_forest_area_m2": 0,
            "raw_outside_forest_mask_m2": 183.873,
        }
        self.helpers["_region_mask_component_tile_batch"].return_value = self.coverage
        self.overlap = {
            "measured_tiles": 2,
            "raw_tiles_intersect_nonforest_exclusion": 2,
            "interior_tiles_intersect_nonforest_exclusion": 0,
            "raw_overlap_m2": 183.873,
            "interior_overlap_m2": 0,
        }
        self.helpers["_source_mask_cache_exclusion_tile_batch"].return_value = self.overlap
        self.db = object()

    def report(self, **kwargs):
        return forest_clip_report(
            self.db, region_code="TR-SMALL", grid_version="small-grid", **kwargs
        )

    def test_edge_only_overlap_is_reported_and_passes_existing_tolerance(self):
        report = self.report()

        self.assertEqual(report["total_nonforest_exclusion_overlap_m2"], 183.873)
        self.assertEqual(report["interior_total_nonforest_exclusion_overlap_m2"], 0)
        self.assertEqual(report["tiles_intersect_nonforest_exclusion"], 2)
        self.assertEqual(report["interior_tiles_intersect_nonforest_exclusion"], 0)
        self.assertEqual(report["nonforest_exclusion_measurement"]["status"], "measured")
        self.assertFalse(report["nonforest_exclusion_measurement"]["independent_proof_required"])
        self.assertEqual(report["mask_edge_tolerance_m"], 1.0)
        self.assertTrue(report["source_mask_cache_used"])
        self.assertTrue(report["region_mask_components_used"])
        self.assertTrue(report["checks"]["nonforest_exclusion_measured"])
        self.assertTrue(report["checks"]["no_cells_intersect_nonforest_exclusion"])
        self.assertTrue(report["checks"]["ready"])

    def test_interior_overlap_rejects_previous_false_green(self):
        # Coverage against the accepted component mask still passes. Independent
        # exclusion evidence must reject a mask that retained nonforest interior.
        self.overlap.update(
            raw_overlap_m2=1800,
            interior_overlap_m2=1200,
            interior_tiles_intersect_nonforest_exclusion=1,
        )

        report = self.report()

        self.assertTrue(report["checks"]["all_cells_inside_forest_mask"])
        self.assertTrue(report["checks"]["forest_mask_covered_by_clipped_grid"])
        self.assertTrue(report["checks"]["nonforest_exclusion_measured"])
        self.assertEqual(report["total_nonforest_exclusion_overlap_m2"], 1800)
        self.assertEqual(report["interior_total_nonforest_exclusion_overlap_m2"], 1200)
        self.assertFalse(report["checks"]["no_cells_intersect_nonforest_exclusion"])
        self.assertFalse(report["checks"]["ready"])

    def test_tile_counts_are_never_reported_as_cell_counts(self):
        self.coverage.update(tiles_outside_forest_mask=1, outside_forest_mask_m2=0.1)

        report = self.report()

        self.assertEqual(report["cell_count"], 23553)
        self.assertEqual(report["tiles_outside_forest_mask"], 1)
        self.assertEqual(report["raw_tiles_outside_forest_mask"], 2)
        self.assertEqual(report["tiles_intersect_nonforest_exclusion"], 2)
        for key in (
            "cells_outside_forest_mask",
            "raw_cells_outside_forest_mask",
            "cells_intersect_nonforest_exclusion",
        ):
            self.assertIsNone(report[key], key)

    def test_missing_required_cache_is_unmeasured_and_not_ready(self):
        for counts in ({}, {"osm_nonforest_exclusion": 0}):
            with self.subTest(counts=counts):
                self.helpers["_source_mask_cache_counts"].return_value = counts

                report = self.report()

                self.assertIsNone(report["total_nonforest_exclusion_overlap_m2"])
                self.assertIsNone(report["interior_total_nonforest_exclusion_overlap_m2"])
                self.assertIsNone(report["tiles_intersect_nonforest_exclusion"])
                self.assertEqual(report["nonforest_exclusion_measurement"]["status"], "unmeasured")
                self.assertTrue(report["nonforest_exclusion_measurement"]["independent_proof_required"])
                self.assertIn("independent exclusion proof", report["nonforest_exclusion_measurement"]["note"])
                self.assertFalse(report["checks"]["source_mask_cache_has_required_layers"])
                self.assertFalse(report["checks"]["rebuilt_nonforest_difference_available"])
                self.assertFalse(report["checks"]["no_cells_intersect_nonforest_exclusion"])
                self.assertFalse(report["checks"]["ready"])
        self.helpers["_source_mask_cache_exclusion_tile_batch"].assert_not_called()

    def test_empty_exclusion_configuration_cannot_vacuously_pass(self):
        self.metadata["forest_mask"]["difference_source_layers"] = []

        report = self.report()

        self.assertIsNone(report["total_nonforest_exclusion_overlap_m2"])
        self.assertFalse(report["checks"]["nonforest_exclusion_measured"])
        self.assertFalse(report["checks"]["no_cells_intersect_nonforest_exclusion"])
        self.assertFalse(report["checks"]["ready"])

    def test_incomplete_measurement_cannot_report_measured_zero(self):
        self.overlap.update(measured_tiles=1, raw_overlap_m2=0)

        report = self.report()

        self.assertEqual(report["nonforest_exclusion_measurement"]["measured_tiles"], 1)
        self.assertIsNone(report["total_nonforest_exclusion_overlap_m2"])
        self.assertFalse(report["checks"]["nonforest_exclusion_measured"])
        self.assertFalse(report["checks"]["ready"])

    def test_no_exclusion_in_scope_is_a_measured_zero(self):
        self.overlap.update(raw_tiles_intersect_nonforest_exclusion=0, raw_overlap_m2=0)

        report = self.report()

        self.assertEqual(report["total_nonforest_exclusion_overlap_m2"], 0)
        self.assertEqual(report["tiles_intersect_nonforest_exclusion"], 0)
        self.assertTrue(report["checks"]["nonforest_exclusion_measured"])
        self.assertTrue(report["checks"]["ready"])

    def test_required_layers_include_region_and_run_exclusions(self):
        self.context["run_metadata"]["exclude_source_layers"] = ["esa_worldcover_nonforest_exclusion"]

        report = self.report()

        self.assertEqual(
            report["nonforest_exclusion_measurement"]["source_layers"],
            ["esa_worldcover_nonforest_exclusion", "osm_nonforest_exclusion"],
        )
        self.assertIsNone(report["total_nonforest_exclusion_overlap_m2"])
        self.assertFalse(report["checks"]["source_mask_cache_has_required_layers"])
        self.assertFalse(report["checks"]["ready"])

    def test_batches_measure_all_tiles_and_sum_overlap(self):
        self.grid_run["total_tiles"] = 3
        self.helpers["_region_mask_component_tile_batch"].side_effect = [
            self.coverage,
            dict(self.coverage, tile_count=1, completed_tiles=1),
        ]
        self.helpers["_source_mask_cache_exclusion_tile_batch"].side_effect = [
            self.overlap,
            dict(self.overlap, measured_tiles=1, raw_tiles_intersect_nonforest_exclusion=1, raw_overlap_m2=2),
        ]

        report = self.report(tile_batch_size=2)

        self.assertEqual(report["total_nonforest_exclusion_overlap_m2"], 185.873)
        self.assertEqual(report["tiles_intersect_nonforest_exclusion"], 3)
        self.assertEqual(report["nonforest_exclusion_measurement"]["measured_tiles"], 3)
        self.assertTrue(report["checks"]["ready"])
        for helper in ("_region_mask_component_tile_batch", "_source_mask_cache_exclusion_tile_batch"):
            calls = self.helpers[helper].call_args_list
            self.assertEqual([call.kwargs["tile_offset"] for call in calls], [0, 2])
            self.assertEqual([call.kwargs["tile_limit"] for call in calls], [2, 2])

    def test_measurement_uses_cache_source_scope_and_grid_projection(self):
        self.metadata["forest_mask"]["source_mask_cache"] = {
            "source_region_code": "TR-CACHE-SOURCE",
            "projected_srid": 6933,
        }

        self.report(tile_batch_size=10)

        self.helpers["_source_mask_cache_counts"].assert_called_once_with(
            self.db,
            source_region_code="TR-CACHE-SOURCE",
            projected_srid=6933,
            source_layers=["osm_nonforest_exclusion"],
        )
        self.helpers["_source_mask_cache_exclusion_tile_batch"].assert_called_once_with(
            self.db,
            run_id=7,
            region_id=9,
            grid_version="small-grid",
            projected_srid=6933,
            source_region_code="TR-CACHE-SOURCE",
            exclusion_layers=["osm_nonforest_exclusion"],
            tile_limit=10,
            tile_offset=0,
        )

    def test_exclusion_area_gate_preserves_existing_scale_tolerance(self):
        for source_area in (100_000, 200_000_000):
            tolerance = _source_mask_scale_tolerance_m2(source_area)
            self.coverage["source_mask_area_m2"] = source_area
            for interior, expected in ((tolerance, True), (tolerance + 0.01, False)):
                with self.subTest(source_area=source_area, interior=interior):
                    self.overlap.update(raw_overlap_m2=interior, interior_overlap_m2=interior)

                    report = self.report()

                    self.assertEqual(report["source_mask_scale_tolerance_m2"], round(tolerance, 6))
                    self.assertEqual(report["checks"]["no_cells_intersect_nonforest_exclusion"], expected)
                    self.assertEqual(report["checks"]["ready"], expected)

    def test_nonpositive_batch_size_is_rejected_before_querying(self):
        for size in (0, -1):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "tile_batch_size"):
                self.report(tile_batch_size=size)
        self.helpers["_load_clip_context"].assert_not_called()


class Stage3ExclusionTileQueryTest(unittest.TestCase):
    def test_exclusion_query_is_bounded_indexed_and_measures_raw_and_interior(self):
        db = Mock()
        expected = {"measured_tiles": 2, "raw_overlap_m2": 183.873, "interior_overlap_m2": 0}
        db.execute.return_value.mappings.return_value.one.return_value = expected

        result = _source_mask_cache_exclusion_tile_batch(
            db,
            run_id=7,
            region_id=9,
            grid_version="small-grid",
            projected_srid=6933,
            source_region_code="TR-SOURCE",
            exclusion_layers=["osm_nonforest_exclusion"],
            tile_limit=2,
            tile_offset=4,
        )

        self.assertEqual(result, expected)
        statement, params = db.execute.call_args.args
        sql = str(statement)
        self.assertEqual(set(statement.compile().params), set(params))
        self.assertEqual(params["mask_edge_tolerance_m"], MASK_EDGE_TOLERANCE_M)
        self.assertEqual(params["mask_edge_tolerance_m"], 1.0)
        self.assertEqual(params["tile_offset"], 4)
        self.assertEqual(params["tile_limit"], 2)
        for clause in (
            "WHERE tile.run_id = :run_id",
            "LIMIT :tile_limit OFFSET :tile_offset",
            "cache.source_region_code = :source_region_code",
            "cache.projected_srid = :projected_srid",
            "cache.source_layer = ANY(CAST(:exclusion_layers AS text[]))",
            "cache.geom_projected && tile.halo_geom",
            "cell.region_id = :region_id",
            "cell.grid_version = :grid_version",
            "cell.geom && tile.geom_4326",
            "AS raw_overlap_m2",
            "AS interior_overlap_m2",
        ):
            self.assertIn(clause, sql)
        self.assertNotIn("stage3_region_mask_components", sql)
        self.assertNotIn("ST_PointOnSurface", sql)
        self.assertNotIn("tile_seam_tolerance_m", params)

        # Erode the tile union of cache fragments, never individual shards or
        # a mask clipped at the tile edge: either would hide interior overlaps.
        self.assertIn("ST_Expand(geom, :mask_edge_tolerance_m) AS halo_geom", sql)
        exclusion_sql = sql.split("exclusion_tile_mask AS (")[1].split("tile_cells AS (")[0]
        self.assertIn("ST_UnaryUnion(ST_Collect(", exclusion_sql)
        self.assertIn("tile.halo_geom)", exclusion_sql)
        self.assertIn("GROUP BY tile.id", exclusion_sql)
        self.assertIn("ST_Buffer(geom, -:mask_edge_tolerance_m) AS interior_geom", exclusion_sql)
        self.assertLess(exclusion_sql.index("ST_UnaryUnion"), exclusion_sql.index("ST_Buffer"))
        self.assertIn("ST_Intersection(\n                            ST_MakeValid(ST_Transform", sql)
        self.assertIn("tile.geom\n", sql)
        self.assertIn("LEFT JOIN exclusion_interior ON exclusion_interior.id = tile.id", sql)


if __name__ == "__main__":
    unittest.main()
