import inspect
import unittest

from app.api import stage1


class Stage1ApiHelperTest(unittest.TestCase):
    def test_stage1_params_resolves_layer_a_source_region(self):
        source = inspect.getsource(stage1.stage1_params)

        self.assertIn("layer_a_region_code", source)
        self.assertIn("_layer_a_region_code(active_region_code", source)

    def test_stage1_layer_a_reads_use_source_region_scope(self):
        source = inspect.getsource(stage1)

        self.assertIn("attributes->>'region_code' = :layer_a_region_code", source)
        self.assertIn("source_checks.region_code = :layer_a_region_code", source)
        self.assertIn("WHERE region_code = :layer_a_region_code", source)

    def test_stage1_summary_unverified_count_reuses_grid_total(self):
        source = inspect.getsource(stage1.get_summary)

        self.assertIn('CAST(:grid_cells AS bigint)', source)
        self.assertNotIn("NOT EXISTS", source)

    def test_stage1_summary_uses_completed_grid_run_count_before_counting_cells(self):
        source = inspect.getsource(stage1.get_summary)

        self.assertIn("latest_completed_grid", source)
        self.assertIn("stage3_grid_generation_runs", source)
        self.assertIn("(SELECT inserted_cells FROM latest_completed_grid)", source)

    def test_stage1_summary_fails_closed_for_missing_scope(self):
        source = inspect.getsource(stage1.get_summary)

        self.assertIn('HTTPException(status_code=404, detail="Region scope not found")', source)
        self.assertIn('HTTPException(status_code=404, detail="Grid version not found for region scope")', source)
        self.assertIn('summary["region_code"] = params["region_code"]', source)
        self.assertIn('summary["grid_exists"]', source)

    def test_grid_cells_endpoint_filters_by_region_id_before_ordering(self):
        source = inspect.getsource(stage1.list_grid_cells)

        self.assertIn("cell.region_id = (", source)
        self.assertIn("WHERE code = :region_code", source)
        self.assertIn("ORDER BY cell.id", source)


if __name__ == "__main__":
    unittest.main()
