import unittest
import inspect
import io
import sys
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from app.commands import run_stage3_grid_generation_batched as grid_command
from app.commands.run_stage3_grid_generation import _require_legacy_direct_grid_not_full
from app.services.grid import generate_region_grid, generate_test_grid
from app.services.stage3_grid_options import (
    projected_srid_for_lon_lat,
    validate_existing_run,
    validate_generation_args,
)
from app.commands.run_stage3_grid_generation_batched import (
    _adopt_existing_grid,
    _configure_forest_mask_grid_options,
    _ensure_tile_queue,
    _existing_grid_checks,
    _insert_tile_cells,
    _parse_source_layers,
    _source_mask_cache_metadata,
    _validate_source_mask_cache,
    _validate_existing_exclusion_options,
    _validate_region_hash,
    _validate_source_geometry_fingerprint,
    _update_run_forest_metadata,
    _validate_legacy_completed_run,
)
from app.services.forest_scope import (
    forest_region_metadata,
    public_forest_mask_metadata_ready,
    require_forest_only_region,
    validate_forest_mask_source_layers,
)
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_V9_GRID_VERSION,
    STAGE3_PUBLIC_V9_REGION_CODE,
)


class RecordingDb:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def one_or_none(self):
        return self.value


class RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class AdoptGridDb(RecordingDb):
    def __init__(self, tile_count=0, checks=None, region_hash="current-region-hash"):
        super().__init__()
        self.tile_count = tile_count
        self.region_hash = region_hash
        self.checks = checks or {
            "cell_count": 533,
            "invalid_cells": 0,
            "outside_cells": 0,
            "area_mismatch_cells": 0,
            "coverage_ratio": 0.98,
        }

    def execute(self, statement, params=None):
        super().execute(statement, params)
        sql = str(statement)
        if "ST_AsEWKB" in sql:
            return ScalarResult(self.region_hash)
        if "FROM stage3_grid_generation_tiles" in sql:
            return ScalarResult(self.tile_count)
        if "coverage_ratio" in sql:
            return ScalarResult(self.checks)
        return ScalarResult(None)


class InsertTileDb(RecordingDb):
    def execute(self, statement, params=None):
        super().execute(statement, params)
        return ScalarResult(0)


class GridCommandDb(AdoptGridDb):
    def __init__(self, final_status="completed"):
        super().__init__()
        self.final_status = final_status
        self.status = "running"
        self.commits = 0
        self.rollbacks = 0
        self.persisted_statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self.commits += 1
        self.persisted_statements.extend(self.statements)

    def rollback(self):
        self.rollbacks += 1

    def execute(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_xact_lock" in sql:
            self.statements.append((sql, params))
            return ScalarResult(True)
        if "FOR UPDATE OF tile SKIP LOCKED" in sql:
            self.statements.append((sql, params))
            return ScalarResult(None)
        if "UPDATE stage3_grid_generation_runs run" in sql:
            self.status = self.final_status
        if "SELECT status FROM stage3_grid_generation_runs" in sql:
            self.statements.append((sql, params))
            return ScalarResult(self.status)
        return super().execute(statement, params)


class SourceMaskCacheDb(RecordingDb):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def execute(self, statement, params=None):
        super().execute(statement, params)
        return RowsResult(self.rows)


class RegionMetadataDb(RecordingDb):
    def __init__(self, metadata):
        super().__init__()
        self.metadata = metadata

    def execute(self, statement, params=None):
        super().execute(statement, params)
        return ScalarResult({"metadata": self.metadata})


class GenerateRegionGridDb(RecordingDb):
    def __init__(self):
        super().__init__()
        self.counts = [0, 0, 0, 2]

    def execute(self, statement, params=None):
        super().execute(statement, params)
        sql = str(statement)
        if "pg_try_advisory_xact_lock" in sql:
            return ScalarResult(True)
        if "SELECT id, code" in sql and "FROM regions" in sql:
            return ScalarResult({"id": 2, "code": "TR-FOREST"})
        if "SELECT count(*)" in sql:
            return ScalarResult(self.counts.pop(0) if self.counts else 0)
        return ScalarResult(None)


class Stage3GridGenerationTest(unittest.TestCase):
    def test_legacy_direct_grid_rejects_canonical_full_v9(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy Stage 3 direct grid generation is disabled"):
            _require_legacy_direct_grid_not_full(
                region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                grid_version="alias-grid",
            )

        with self.assertRaisesRegex(RuntimeError, "Legacy Stage 3 direct grid generation is disabled"):
            _require_legacy_direct_grid_not_full(
                region_code="TR-TURKEY-ALIAS",
                grid_version=STAGE3_PUBLIC_V9_GRID_VERSION,
            )

    def test_legacy_direct_grid_rejects_national_scale_alias(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy Stage 3 direct grid generation is disabled"):
            _require_legacy_direct_grid_not_full(
                region_code="TR-TURKEY-ALIAS",
                grid_version="alias-grid",
                scope_area_ha=1_250_000,
            )

    def test_legacy_direct_grid_allows_small_pilot_scope(self):
        _require_legacy_direct_grid_not_full(
            region_code="TR-PILOT",
            grid_version="pilot-grid",
            scope_area_ha=533,
        )

    def test_resume_workers_skip_full_source_mask_cache_validation(self):
        source = inspect.getsource(grid_command.main)

        self.assertIn("if args.use_source_mask_cache and not args.resume:", source)
        self.assertLess(source.index("run = _ensure_run"), source.index("_validate_source_mask_cache"))

    def test_full_turkey_v9_grid_args_guard_runs_before_generation(self):
        source = inspect.getsource(grid_command.main)

        self.assertIn("require_stage3_public_v9_full_grid_args", source)
        self.assertLess(source.index("require_stage3_public_v9_full_grid_args"), source.index("validate_generation_args"))

    def test_existing_run_requires_matching_generation_parameters(self):
        run = {
            "grid_size_m": 100,
            "projected_srid": 32636,
            "tile_size_m": 10000,
        }
        args = Namespace(grid_size_m=100, projected_srid=32637, tile_size_m=10000)

        with self.assertRaisesRegex(RuntimeError, "projected_srid"):
            validate_existing_run(run, args)

    def test_generation_args_reject_invalid_retry_count(self):
        args = Namespace(
            grid_size_m=100,
            projected_srid=32636,
            tile_size_m=10000,
            max_tiles=0,
            max_tile_attempts=0,
            max_failed_tiles=0,
        )

        with self.assertRaisesRegex(ValueError, "max_tile_attempts"):
            validate_generation_args(args)

    def test_generation_args_reject_invalid_failed_tile_limit(self):
        args = Namespace(
            grid_size_m=100,
            projected_srid=32636,
            tile_size_m=10000,
            max_tiles=0,
            max_tile_attempts=3,
            max_failed_tiles=-1,
        )

        with self.assertRaisesRegex(ValueError, "max_failed_tiles"):
            validate_generation_args(args)

    def test_generation_args_reject_invalid_statement_timeout(self):
        args = Namespace(
            grid_size_m=100,
            projected_srid=32636,
            tile_size_m=10000,
            max_tiles=0,
            max_tile_attempts=3,
            max_failed_tiles=0,
            statement_timeout_ms=-1,
        )

        with self.assertRaisesRegex(ValueError, "statement_timeout_ms"):
            validate_generation_args(args)

    def test_zero_failed_tile_limit_is_enforced(self):
        source = inspect.getsource(grid_command.main)

        self.assertIn("if failed_total > args.max_failed_tiles:", source)
        self.assertNotIn("if args.max_failed_tiles and failed_total", source)

    def test_per_tile_statement_timeout_is_set_locally(self):
        source = inspect.getsource(grid_command.main)
        helper = inspect.getsource(grid_command._configure_tile_statement_timeout)

        self.assertIn("_configure_tile_statement_timeout(db, args)", source)
        self.assertIn("set_config('statement_timeout'", helper)
        self.assertIn("true", helper)

    def test_generation_args_reject_oversized_tiles(self):
        args = Namespace(
            grid_size_m=100,
            projected_srid=32636,
            tile_size_m=100000,
            max_tiles=0,
            max_tile_attempts=3,
            max_failed_tiles=0,
        )

        with self.assertRaisesRegex(ValueError, "tile_size_m"):
            validate_generation_args(args)

    def test_generation_args_reject_non_aligned_tile_size(self):
        args = Namespace(
            grid_size_m=128,
            projected_srid=32636,
            tile_size_m=10000,
            max_tiles=0,
            max_tile_attempts=3,
            max_failed_tiles=0,
        )

        with self.assertRaisesRegex(ValueError, "integer multiple"):
            validate_generation_args(args)

    def test_projected_srid_can_be_selected_from_lon_lat(self):
        self.assertEqual(projected_srid_for_lon_lat(32.8, 40.9), 32636)
        self.assertEqual(projected_srid_for_lon_lat(42.0, 39.0), 32638)

    def test_tile_queue_sql_uses_bbox_extent_without_expensive_region_intersects(self):
        db = RecordingDb()
        args = Namespace(projected_srid=6933, tile_size_m=10000)

        _ensure_tile_queue(db, run_id=1, region_id=2, args=args)

        tile_queue_sql = db.statements[1][0]
        self.assertIn("FROM tiles", tile_queue_sql)
        self.assertNotIn("ST_Intersects(tile.geom, region.geom)", tile_queue_sql)

    def test_source_mask_tile_queue_uses_bbox_tiles_without_full_region_transform(self):
        db = RecordingDb()
        args = Namespace(
            region_code="TR-FOREST",
            projected_srid=6933,
            tile_size_m=10000,
            mask_source_layers=["osm_forest_mask"],
            mask_intersect_source_layers=[],
            mask_source_region_code="TR-SOURCE",
            grid_from_source_mask=True,
        )

        _ensure_tile_queue(db, run_id=1, region_id=2, args=args)

        tile_queue_sql, params = db.statements[1]
        self.assertIn("region_extent_4326 AS", tile_queue_sql)
        self.assertIn("WHERE code = :mask_source_region_code", tile_queue_sql)
        self.assertIn("FROM tiles", tile_queue_sql)
        self.assertIn("WHERE EXISTS", tile_queue_sql)
        self.assertIn("parcel.source_layer = ANY(:mask_source_layers)", tile_queue_sql)
        self.assertIn("NOT :use_intersect_mask", tile_queue_sql)
        self.assertNotIn("ST_Transform(geom, :projected_srid) AS geom\n                FROM regions", tile_queue_sql)
        self.assertNotIn("FROM ogm_parcels source", tile_queue_sql)
        self.assertNotIn("tile.geom && region.geom", tile_queue_sql)
        self.assertEqual(params["tile_size_m"], 10000)
        self.assertEqual(params["mask_source_region_code"], "TR-SOURCE")

    def test_batched_grid_cells_store_clipped_forest_geometry(self):
        db = InsertTileDb()
        args = Namespace(region_code="TR-FOREST")
        tile = {
            "region_id": 2,
            "grid_version": "forest-v1",
            "grid_size_m": 100,
            "projected_srid": 32636,
            "min_x": 0,
            "min_y": 0,
            "max_x": 1000,
            "max_y": 1000,
        }

        _insert_tile_cells(db, tile, args)

        insert_sql = db.statements[0][0]
        self.assertIn("ST_Intersects(square.geom, region.geom)", insert_sql)
        self.assertIn("ST_Intersection(square_geom, region.geom)", insert_sql)
        self.assertIn("region_clipped AS", insert_sql)
        self.assertIn("ST_Intersection(ST_Transform(valid_clipped.geom, 4326), region.geom_4326)", insert_sql)
        self.assertIn("ST_MakeValid(ST_Transform(geom, 4326))", insert_sql)
        self.assertNotIn("ST_SnapToGrid", insert_sql)
        self.assertIn("ST_Area(", insert_sql)
        self.assertIn("GREATEST", insert_sql)
        self.assertIn("0.000001", insert_sql)
        self.assertNotIn("ST_CoveredBy(square.geom, region.geom)", insert_sql)
        self.assertNotIn("ST_Dump", insert_sql)
        self.assertNotIn("candidate.rank = 1", insert_sql)
        self.assertNotIn("ST_Contains(region.geom, ST_Centroid(square.geom))", insert_sql)

    def test_batched_grid_cells_can_subtract_nonforest_sources_per_tile(self):
        db = InsertTileDb()
        args = Namespace(region_code="TR-FOREST", exclude_source_layers=["osm_nonforest_exclusion"])
        tile = {
            "region_id": 2,
            "grid_version": "forest-v1",
            "grid_size_m": 100,
            "projected_srid": 32636,
            "min_x": 0,
            "min_y": 0,
            "max_x": 1000,
            "max_y": 1000,
        }

        _insert_tile_cells(db, tile, args)

        insert_sql, params = db.statements[0]
        self.assertIn("exclusion_mask AS", insert_sql)
        self.assertIn("ST_Difference(ST_Intersection(square_geom, region.geom), exclusion_mask.geom)", insert_sql)
        self.assertTrue(params["use_exclusion_mask"])
        self.assertEqual(params["exclude_source_layers"], ["osm_nonforest_exclusion"])

    def test_batched_grid_cells_can_use_source_mask_per_tile(self):
        db = InsertTileDb()
        args = Namespace(
            region_code="TR-FOREST",
            mask_source_layers=["osm_forest_mask"],
            mask_intersect_source_layers=[],
            mask_source_region_code="TR-SOURCE",
            exclude_source_layers=["osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
            grid_from_source_mask=True,
        )
        tile = {
            "region_id": 2,
            "grid_version": "forest-v1",
            "grid_size_m": 100,
            "projected_srid": 6933,
            "min_x": 0,
            "min_y": 0,
            "max_x": 1000,
            "max_y": 1000,
        }

        _insert_tile_cells(db, tile, args)

        insert_sql, params = db.statements[0]
        self.assertIn("source_mask AS", insert_sql)
        self.assertIn("tile_mask AS", insert_sql)
        self.assertIn("canonical_region_mask AS", insert_sql)
        self.assertIn("clean_tile_mask AS", insert_sql)
        self.assertIn("ST_Intersection(tile_mask.geom, canonical_region_mask.geom)", insert_sql)
        self.assertIn("ST_SquareGrid(:grid_size_m, clean_tile_mask.geom)", insert_sql)
        self.assertIn("ST_Difference(forest_mask.geom, exclusion_mask.geom)", insert_sql)
        self.assertNotIn("ST_Area(component.geom) / 10000.0 >= :min_mask_component_area_ha", insert_sql)
        self.assertNotIn("region_clipped AS", insert_sql)
        self.assertIn("ST_MakeValid(ST_Transform(valid_clipped.geom, 4326))", insert_sql)
        self.assertNotIn("ST_SnapToGrid(ST_Transform(valid_clipped.geom, 4326)", insert_sql)
        self.assertIn("ST_Area(", insert_sql)
        self.assertIn("GREATEST", insert_sql)
        self.assertIn("0.000001", insert_sql)
        self.assertNotIn("ST_Intersection(square_geom, region.geom)", insert_sql)
        self.assertEqual(params["mask_source_layers"], ["osm_forest_mask"])
        self.assertEqual(params["mask_source_region_code"], "TR-SOURCE")

    def test_batched_grid_cells_can_use_prepared_source_mask_cache(self):
        db = InsertTileDb()
        args = Namespace(
            region_code="TR-FOREST",
            mask_source_layers=["osm_forest_mask"],
            mask_intersect_source_layers=[],
            mask_source_region_code="TR-SOURCE",
            exclude_source_layers=["osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
            use_source_mask_cache=True,
        )
        tile = {
            "region_id": 2,
            "grid_version": "forest-v1",
            "grid_size_m": 100,
            "projected_srid": 6933,
            "min_x": 0,
            "min_y": 0,
            "max_x": 1000,
            "max_y": 1000,
        }

        _insert_tile_cells(db, tile, args)

        insert_sql, params = db.statements[0]
        self.assertIn("FROM stage3_source_mask_cache cache", insert_sql)
        self.assertIn("cache.geom_projected && tile.geom", insert_sql)
        self.assertIn("canonical_region_mask AS", insert_sql)
        self.assertIn("clean_tile_mask AS", insert_sql)
        self.assertIn("ST_Intersection(tile_mask.geom, canonical_region_mask.geom)", insert_sql)
        self.assertIn("ST_SquareGrid(:grid_size_m, clean_tile_mask.geom)", insert_sql)
        self.assertIn("ST_Difference(forest_mask.geom, exclusion_mask.geom)", insert_sql)
        self.assertNotIn("region_clipped AS", insert_sql)
        self.assertIn("ST_MakeValid(ST_Transform(valid_clipped.geom, 4326))", insert_sql)
        self.assertNotIn("ST_SnapToGrid(ST_Transform(valid_clipped.geom, 4326)", insert_sql)
        self.assertNotIn("ST_Transform(ST_MakeValid(parcel.geom), :projected_srid)", insert_sql)
        self.assertEqual(params["mask_source_region_code"], "TR-SOURCE")

    def test_batched_grid_cells_can_use_final_region_mask_components(self):
        db = InsertTileDb()
        args = Namespace(
            region_code="TR-FOREST-V4",
            use_region_mask_components=True,
            forest_region_metadata={
                "forest_mask": {
                    "mask_version": "TR-FOREST-V4",
                }
            },
        )
        tile = {
            "region_id": 2,
            "grid_version": "forest-v4",
            "grid_size_m": 100,
            "projected_srid": 6933,
            "min_x": 0,
            "min_y": 0,
            "max_x": 1000,
            "max_y": 1000,
        }

        _insert_tile_cells(db, tile, args)

        insert_sql, params = db.statements[0]
        self.assertIn("FROM stage3_region_mask_components component", insert_sql)
        self.assertIn("component.geom_projected && tile.geom", insert_sql)
        self.assertIn("component.mask_version = :mask_version", insert_sql)
        self.assertIn("ST_SquareGrid(:grid_size_m, clean_tile_mask.geom)", insert_sql)
        self.assertNotIn("FROM stage3_source_mask_cache cache", insert_sql)
        self.assertNotIn("ST_Transform(ST_MakeValid(region.geom), :projected_srid)", insert_sql)
        self.assertEqual(params["mask_version"], "TR-FOREST-V4")

    def test_source_mask_cache_validation_requires_every_source_layer(self):
        db = SourceMaskCacheDb(
            [
                {"source_layer": "osm_forest_mask", "components": 12, "bad_components": 0},
                {"source_layer": "osm_nonforest_exclusion", "components": 8, "bad_components": 0},
            ]
        )
        args = Namespace(
            region_code="TR-FOREST",
            projected_srid=6933,
            mask_source_layers=["osm_forest_mask"],
            mask_intersect_source_layers=[],
            mask_source_region_code="TR-SOURCE",
            exclude_source_layers=["osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
            use_source_mask_cache=True,
        )

        with self.assertRaisesRegex(RuntimeError, "esa_worldcover_nonforest_exclusion"):
            _validate_source_mask_cache(db, args)

    def test_source_mask_cache_metadata_keeps_public_source_provenance(self):
        args = Namespace(
            region_code="TR-FOREST",
            projected_srid=6933,
            mask_source_layers=["osm_forest_mask"],
            mask_intersect_source_layers=[],
            mask_source_region_code="TR-SOURCE",
            exclude_source_layers=["osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
            use_source_mask_cache=True,
        )

        metadata = _source_mask_cache_metadata(args)

        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata["source_region_code"], "TR-SOURCE")
        self.assertEqual(
            metadata["source_layers"],
            ["osm_forest_mask", "osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
        )
        self.assertEqual(metadata["projected_srid"], 6933)

    def test_source_mask_cache_metadata_tracks_clean_mask_policy(self):
        args = Namespace(
            region_code="TR-FOREST",
            projected_srid=6933,
            mask_source_layers=["osm_forest_mask"],
            mask_intersect_source_layers=[],
            mask_source_region_code="TR-SOURCE",
            exclude_source_layers=["osm_nonforest_exclusion"],
            use_source_mask_cache=True,
            min_mask_component_area_ha=1.0,
        )

        metadata = _source_mask_cache_metadata(args)

        self.assertEqual(metadata["clean_mask_policy"]["min_component_area_ha"], 1.0)

    def test_forest_mask_metadata_configures_source_grid_options(self):
        args = Namespace(
            region_code="TR-FOREST",
            exclude_source_layers=[],
            forest_region_metadata={
                "forest_only": True,
                "forest_mask": {
                    "source_layers": ["osm_forest_mask"],
                    "source_region_code": "TR-SOURCE",
                    "intersect_source_layers": [],
                    "difference_source_layers": [
                        "osm_nonforest_exclusion",
                        "esa_worldcover_nonforest_exclusion",
                    ],
                    "clean_mask_policy": {
                        "method": "component_area_filter_before_grid_generation",
                        "min_component_area_ha": 1.0,
                        "applies_before_grid_insert": True,
                    },
                },
            },
            min_mask_component_area_ha=0,
            grid_from_source_mask=False,
            use_source_mask_cache=False,
        )

        _configure_forest_mask_grid_options(args)

        self.assertEqual(args.mask_source_layers, ["osm_forest_mask"])
        self.assertEqual(args.mask_source_region_code, "TR-SOURCE")
        self.assertEqual(args.exclude_source_layers, [])
        self.assertEqual(args.min_mask_component_area_ha, 1.0)

    def test_parse_source_layers_trims_empty_values(self):
        self.assertEqual(_parse_source_layers("osm_nonforest_exclusion, ,esa_worldcover_nonforest_exclusion"), [
            "osm_nonforest_exclusion",
            "esa_worldcover_nonforest_exclusion",
        ])

    def test_existing_grid_resume_rejects_changed_exclusion_layers(self):
        run = {"metadata": {"exclude_source_layers": [], "exclusion_method": ""}}
        args = Namespace(exclude_source_layers=["osm_nonforest_exclusion"])

        with self.assertRaisesRegex(RuntimeError, "exclude_source_layers"):
            _validate_existing_exclusion_options(run, args)

    def test_existing_grid_resume_accepts_matching_exclusion_layers(self):
        run = {
            "status": "running",
            "metadata": {
                "exclude_source_layers": ["osm_nonforest_exclusion"],
                "exclusion_method": "per_tile_st_difference",
                "grid_mask_method": "region_geometry",
            }
        }
        args = Namespace(exclude_source_layers=["osm_nonforest_exclusion"])

        _validate_existing_exclusion_options(run, args)

    def test_running_grid_resume_rejects_missing_region_hash(self):
        run = {"status": "running", "metadata": {}}

        with self.assertRaisesRegex(RuntimeError, "region_geometry_hash"):
            _validate_region_hash(run, "current-hash")

    def test_completed_legacy_grid_can_reach_legacy_validation(self):
        run = {"status": "completed", "metadata": {}}

        _validate_region_hash(run, "current-hash")

    def test_exact_geometry_fingerprint_rejects_changed_source_on_completed_resume(self):
        run = {"status": "completed", "metadata": {
            "source_geometry_fingerprint": "original",
            "source_geometry_fingerprint_algorithm": "ewkb-md5-v1",
        }}
        _validate_source_geometry_fingerprint(run, "original")
        with self.assertRaisesRegex(RuntimeError, "Source geometry changed"):
            _validate_source_geometry_fingerprint(run, "one-millimetre-change")

    def test_exact_geometry_fingerprint_rejects_unknown_algorithm(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown source geometry"):
            _validate_source_geometry_fingerprint({"metadata": {
                "source_geometry_fingerprint": "original",
                "source_geometry_fingerprint_algorithm": "future-version",
            }}, "original")

    def test_running_grid_resume_rejects_missing_grid_mask_method(self):
        run = {"status": "failed", "metadata": {"exclude_source_layers": [], "exclusion_method": ""}}
        args = Namespace(exclude_source_layers=[], use_region_mask_components=True)

        with self.assertRaisesRegex(RuntimeError, "grid_mask_method"):
            _validate_existing_exclusion_options(run, args)

    def test_tile_worker_locks_only_tile_rows_for_parallel_resume(self):
        from app.commands import run_stage3_grid_generation_batched as command

        source = "\n".join(str(value) for value in command._next_tile.__code__.co_consts)

        self.assertIn("FOR UPDATE OF tile SKIP LOCKED", source)
        self.assertNotIn("FOR UPDATE SKIP LOCKED", source)

    def test_direct_region_grid_stores_clipped_region_geometry(self):
        db = GenerateRegionGridDb()

        generate_region_grid(db, region_code="TR-FOREST", grid_version="forest-v1")

        create_sql = next(statement for statement, _ in db.statements if "CREATE TEMP TABLE region_grid_candidates" in statement)
        self.assertIn("ST_Intersects(square.geom, region.geom)", create_sql)
        self.assertIn("ST_Intersection(square_geom, region.geom)", create_sql)
        self.assertIn("ST_Intersection(ST_Transform(geom, 4326), geom_4326)", create_sql)
        self.assertIn("geometry(Geometry, 4326)", create_sql)
        self.assertNotIn("ST_CoveredBy(square.geom, region.geom)", create_sql)

    def test_existing_grid_checks_require_full_cell_coverage(self):
        db = AdoptGridDb()
        args = Namespace(grid_version="forest-v1", projected_srid=32636, grid_size_m=100)

        _existing_grid_checks(db, region_id=2, args=args)

        check_sql = db.statements[0][0]
        self.assertIn("ST_CoveredBy(cells.geom, region.geom)", check_sql)
        self.assertNotIn("ST_CoveredBy(ST_PointOnSurface(cells.geom), region.geom)", check_sql)

    def test_existing_grid_adoption_marks_run_without_inserting_cells(self):
        db = AdoptGridDb()
        args = Namespace(grid_version="stage1-test-v1", projected_srid=32636, grid_size_m=100)

        adopted = _adopt_existing_grid(db, run_id=7, region_id=2, args=args)

        self.assertEqual(adopted, 533)
        joined_sql = "\n".join(statement for statement, _ in db.statements)
        self.assertIn("adopted_existing_grid_cells", joined_sql)
        self.assertIn("adoption_checks", joined_sql)
        self.assertIn("region_geometry_hash", joined_sql)
        update_params = db.statements[-1][1]
        self.assertEqual(update_params["existing_cells"], 533)
        self.assertEqual(update_params["region_geometry_hash"], "current-region-hash")
        self.assertNotIn("INSERT INTO grid_cells", joined_sql)

    def test_component_grid_adoption_then_unchanged_resume_preserves_hash_domain(self):
        db = AdoptGridDb()
        args = Namespace(region_code="TR-SMALL", grid_version="small-v1", grid_size_m=100,
                         projected_srid=6933, tile_size_m=10000, resume=True,
                         use_region_mask_components=True)
        with patch.object(grid_command, "_grid_source_hash", return_value="component-hash"), \
             patch.object(grid_command, "_exact_grid_source_hash", return_value="exact-component-hash"), \
             patch.object(grid_command, "_region_geometry_hash", side_effect=AssertionError("wrong hash domain")):
            _adopt_existing_grid(db, run_id=7, region_id=2, args=args)
            stored = db.statements[-1][1]
            self.assertEqual(stored["region_geometry_hash"], "component-hash")
            self.assertEqual(stored["source_geometry_fingerprint"], "exact-component-hash")
            run = {"id": 7, "status": "completed", "grid_size_m": 100,
                   "projected_srid": 6933, "tile_size_m": 10000, "metadata": {
                       "region_geometry_hash": stored["region_geometry_hash"],
                       "source_geometry_fingerprint": stored["source_geometry_fingerprint"],
                       "source_geometry_fingerprint_algorithm": "ewkb-md5-v1",
                       "grid_mask_method": "region_mask_components",
                   }}
            with patch.object(db, "execute", return_value=ScalarResult(run)):
                self.assertEqual(grid_command._ensure_run(db, {"id": 2}, args)["id"], 7)
                with patch.object(grid_command, "_exact_grid_source_hash", return_value="changed"):
                    with self.assertRaisesRegex(RuntimeError, "Source geometry changed"):
                        grid_command._ensure_run(db, {"id": 2}, args)

    def _run_bounded_cli(self, db, flags):
        argv = ["grid", "--region-code", "TR-SMALL", "--grid-version", "small-v1",
                "--projected-srid", "6933", "--max-failed-tiles", "0", *flags]
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(grid_command, "SessionLocal", return_value=db))
            stack.enter_context(patch.object(grid_command, "_region", return_value={
                "id": 2, "code": "TR-SMALL", "area_ha": 4}))
            stack.enter_context(patch.object(grid_command, "require_forest_only_region", return_value={
                "forest_only": True, "forest_mask": {}}))
            stack.enter_context(patch.object(grid_command, "_ensure_run", return_value={"id": 7}))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            grid_command.main()
            return output.getvalue()

    def test_adoption_dry_run_validates_but_does_not_commit(self):
        db = GridCommandDb()
        output = self._run_bounded_cli(db, ["--adopt-existing", "--dry-run"])
        self.assertIn("adoption dry run", output)
        self.assertEqual((db.commits, db.rollbacks), (0, 1))
        self.assertFalse(db.persisted_statements)
        self.assertTrue(any("adoption_checks" in sql for sql, _ in db.statements))

    def test_adoption_without_dry_run_still_commits(self):
        db = GridCommandDb()
        self.assertIn("grid adopted", self._run_bounded_cli(db, ["--adopt-existing"]))
        self.assertEqual((db.commits, db.rollbacks), (1, 0))

    def test_exhausted_failed_resume_raises_after_persisting_failure(self):
        db = GridCommandDb(final_status="failed")
        with self.assertRaisesRegex(RuntimeError, "remains failed"):
            self._run_bounded_cli(db, ["--resume"])
        self.assertEqual(db.status, "failed")
        self.assertEqual(db.commits, 2)  # Initial resume and final failed status.

    def test_no_eligible_tile_can_be_completed_or_waiting_for_other_worker(self):
        for status in ("completed", "running"):
            with self.subTest(status=status):
                db = GridCommandDb(final_status=status)
                self._run_bounded_cli(db, ["--resume"])
                self.assertEqual(db.status, status)

    def test_finish_does_not_fail_retryable_tiles_claimed_by_other_worker(self):
        db = RecordingDb()
        grid_command._finish_run_if_complete(db, "TR-SMALL", "small-v1")
        sql = db.statements[0][0]
        unfinished_work = sql[sql.index("AND NOT EXISTS"):]
        self.assertIn("tile.status = 'pending'", unfinished_work)
        self.assertIn("tile.status = 'failed'", unfinished_work)
        self.assertIn("tile.attempt_count <", unfinished_work)
        self.assertIn("run.metadata->>'max_tile_attempts'", unfinished_work)

    def test_existing_grid_adoption_does_not_replace_real_tile_history(self):
        db = AdoptGridDb(tile_count=2)
        args = Namespace(grid_version="stage1-test-v1", projected_srid=32636, grid_size_m=100)

        with self.assertRaisesRegex(RuntimeError, "real tile execution history"):
            _adopt_existing_grid(db, run_id=7, region_id=2, args=args)

    def test_existing_grid_adoption_rejects_low_coverage(self):
        db = AdoptGridDb(
            checks={
                "cell_count": 533,
                "invalid_cells": 0,
                "outside_cells": 0,
                "area_mismatch_cells": 0,
                "coverage_ratio": 0.72,
            }
        )
        args = Namespace(grid_version="stage1-test-v1", projected_srid=32636, grid_size_m=100)

        with self.assertRaisesRegex(RuntimeError, "cover enough"):
            _adopt_existing_grid(db, run_id=7, region_id=2, args=args)

    def test_legacy_completed_run_without_hash_must_still_match_region(self):
        db = AdoptGridDb(
            checks={
                "cell_count": 533,
                "invalid_cells": 0,
                "outside_cells": 8,
                "area_mismatch_cells": 0,
                "coverage_ratio": 0.98,
            }
        )
        args = Namespace(grid_version="stage1-test-v1", projected_srid=32636, grid_size_m=100)
        run = {"status": "completed", "metadata": {}}

        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            _validate_legacy_completed_run(db, run, region_id=2, args=args)

    def test_existing_run_forest_metadata_can_be_backfilled(self):
        db = RecordingDb()
        args = Namespace(
            forest_region_metadata={
                "forest_only": True,
                "forest_mask": {"source": "public_ogm_layer_a"},
            }
        )

        _update_run_forest_metadata(db, run_id=7, args=args)

        statement, params = db.statements[0]
        self.assertIn("jsonb_build_object", statement)
        self.assertTrue(params["forest_only"])
        self.assertIn("public_ogm_layer_a", params["forest_mask"])

    def test_grid_generation_requires_forest_only_region_metadata(self):
        db = RegionMetadataDb({"stage3_region_source": "turkey_bbox", "forest_only": False})

        with self.assertRaisesRegex(RuntimeError, "forest-only region"):
            require_forest_only_region(db, region_code="TR")

    def test_grid_generation_rejects_public_ogm_without_osm_clip(self):
        db = RegionMetadataDb(
            {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {"source": "public_ogm_layer_a"},
            }
        )

        with self.assertRaisesRegex(RuntimeError, "forest-only region"):
            require_forest_only_region(db, region_code="TR")

    def test_grid_generation_accepts_forest_only_layer_a_region_with_osm_clip(self):
        db = RegionMetadataDb(
            {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {"source": "public_ogm_layer_a_with_osm_forest_clip"},
            }
        )

        metadata = require_forest_only_region(db, region_code="TR")

        self.assertTrue(metadata["forest_only"])

    def test_grid_generation_accepts_public_ogm_osm_union_region(self):
        db = RegionMetadataDb(
            {
                "stage3_region_source": "forest_mask_layer_a",
                "forest_only": True,
                "forest_mask": {
                    "source": "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference"
                },
            }
        )

        metadata = require_forest_only_region(db, region_code="TR")

        self.assertTrue(metadata["forest_only"])

    def test_forest_mask_rejects_unknown_source_layers(self):
        with self.assertRaisesRegex(RuntimeError, "not allowed"):
            validate_forest_mask_source_layers(["raw_unknown_cadastre"])

    def test_public_forest_mask_metadata_tracks_source_quality(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE1-TEST",
            source_layers=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg"],
        )

        self.assertEqual(metadata["forest_mask"]["source"], "public_ogm_layer_a")
        self.assertIn("public_ogm_rest", metadata["forest_mask"]["source_qualities"])
        self.assertFalse(public_forest_mask_metadata_ready(metadata))

    def test_public_forest_mask_metadata_accepts_osm_refinement(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE1-TEST",
            source_layers=["ogm_esatis_bolme", "ogm_yayin_mescere_wms_svg"],
            intersect_source_layers=["osm_forest_mask"],
            difference_source_layers=["osm_nonforest_exclusion"],
        )

        self.assertEqual(
            metadata["forest_mask"]["source"],
            "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
        )
        self.assertEqual(metadata["forest_mask"]["difference_source_layers"], ["osm_nonforest_exclusion"])
        self.assertTrue(public_forest_mask_metadata_ready(metadata))

    def test_public_forest_mask_metadata_requires_real_osm_refinement(self):
        metadata = {
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

        self.assertFalse(public_forest_mask_metadata_ready(metadata))

    def test_public_forest_mask_metadata_rejects_future_official_source(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE1-TEST",
            source_layers=["official_layer_a_vector"],
        )

        self.assertEqual(metadata["forest_mask"]["source"], "official_full_layer_a")
        self.assertFalse(public_forest_mask_metadata_ready(metadata))

    def test_test_grid_region_insert_writes_metadata_after_region_migration(self):
        source = generate_test_grid.__code__.co_consts
        joined = "\n".join(str(value) for value in source)

        self.assertIn("INSERT INTO regions (name, code, country_code, geom, metadata)", joined)
        self.assertIn("'stage3_region_source', 'stage1_test_region'", joined)


if __name__ == "__main__":
    unittest.main()
