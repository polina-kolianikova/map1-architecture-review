import tempfile
import unittest
import inspect
from argparse import Namespace
from pathlib import Path

from app.commands.run_stage3_performance_baseline import _selected_tile
from app.commands.run_stage3_performance_baseline import (
    _asset_count_sql,
    _asset_register_sql,
    _layer_a_intersection_sql,
    _layer_b_mvt_sql,
    _layer_c_mvt_sql,
    _raw_grid_mvt_sql,
    _table_counts,
    write_report,
)
from app.commands import run_stage3_performance_baseline as performance_baseline


class EmptyResult:
    def mappings(self):
        return self

    def first(self):
        return None


class EmptyDb:
    def execute(self, *_args, **_kwargs):
        return EmptyResult()


class SingleRowResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class EvidenceDb:
    def __init__(self):
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        return SingleRowResult({"lon": 32.85, "lat": 39.93})


class Stage3PerformanceBaselineTest(unittest.TestCase):
    def test_selected_tile_reports_missing_region_without_traceback(self):
        args = Namespace(tile_x=None, tile_y=None, tile_z=10)
        params = {"region_code": "TR-MISSING"}

        with self.assertRaisesRegex(SystemExit, "region not found: TR-MISSING"):
            _selected_tile(EmptyDb(), params, args)

    def test_mvt_benchmarks_use_preclipped_cell_geometry(self):
        for query in (_raw_grid_mvt_sql(), _layer_b_mvt_sql(), _layer_c_mvt_sql()):
            self.assertIn("ST_CollectionExtract(ST_MakeValid(cell.geom), 3)", query)
            self.assertNotIn("ST_Intersection(cell.geom, region.geom)", query)
            self.assertNotIn("ST_Transform(cell.geom, 3857)", query)

    def test_selected_tile_prefers_current_evidence_cell(self):
        args = Namespace(tile_x=None, tile_y=None, tile_z=10)
        params = {"region_code": "TR-STAGE3", "grid_version": "stage3-v1"}
        db = EvidenceDb()

        tile = _selected_tile(db, params, args)

        self.assertEqual(tile["source"], "current_evidence_cell")
        self.assertEqual(db.calls, 1)

    def test_performance_layer_a_probes_use_source_region_scope(self):
        intersection_sql = _layer_a_intersection_sql()
        counts_sql = _table_counts.__code__.co_consts
        counts_text = "\n".join(str(value) for value in counts_sql if isinstance(value, str))

        self.assertIn(":layer_a_region_code", intersection_sql)
        self.assertIn(":layer_a_region_code", counts_text)
        self.assertNotIn("parcel.attributes->>'region_code' = :region_code", intersection_sql)

    def test_asset_register_probe_uses_stable_id_order(self):
        query = _asset_register_sql()

        self.assertIn("WITH region_scope AS", query)
        self.assertIn("ORDER BY cell.id", query)
        self.assertIn("scoped_cells AS MATERIALIZED", query)
        self.assertIn("JOIN scoped_cells cell ON cell.id = result.grid_cell_id", query)
        self.assertIn("cell.region_id = (SELECT id FROM region_scope)", query)
        self.assertIn("LIMIT :limit", query)
        self.assertNotIn("JOIN regions region ON region.id = cell.region_id", query)
        self.assertNotIn("JOIN region_scope region ON region.id = cell.region_id", query)
        self.assertNotIn("latest_result.created_at DESC NULLS LAST", query)

    def test_asset_register_count_uses_region_id_scope(self):
        query = _asset_count_sql()

        self.assertIn("stage3_grid_generation_runs", query)
        self.assertIn("run.inserted_cells", query)
        self.assertIn("cell.region_id = (", query)
        self.assertIn("WHERE code = :region_code", query)
        self.assertIn("cell.grid_version = :grid_version", query)

    def test_performance_queries_can_target_partitioned_parent(self):
        grid_table = "grid_cells_partitioned"

        for query in (
            _asset_register_sql(grid_table),
            _asset_count_sql(grid_table),
            _layer_a_intersection_sql(grid_table),
            _raw_grid_mvt_sql(grid_table),
            _layer_b_mvt_sql(grid_table),
            _layer_c_mvt_sql(grid_table),
        ):
            self.assertIn("grid_cells_partitioned", query)
            self.assertNotIn("grid_cells_partitioned_v10", query)

    def test_partitioned_perf_option_is_scope_locked_to_v10(self):
        source = inspect.getsource(performance_baseline.main)

        self.assertIn("args.grid_cells_table == \"grid_cells_partitioned\"", source)
        self.assertIn("V10_REGION_CODE", source)
        self.assertIn("V10_GRID_VERSION", source)

    def test_write_report_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reports" / "stage3-performance.json"

            write_report(path, '{"status":"ok"}')

            self.assertEqual(path.read_text(encoding="utf-8"), '{"status":"ok"}\n')


if __name__ == "__main__":
    unittest.main()
