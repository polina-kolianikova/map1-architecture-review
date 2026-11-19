import inspect
import unittest
from datetime import date

from app.commands import fetch_cdse_ndvi


class Stage1CdseNdviSqlTest(unittest.TestCase):
    def test_load_grid_cells_filters_by_region_id_before_ordering(self):
        source = inspect.getsource(fetch_cdse_ndvi.load_grid_cells)

        self.assertIn("cell.region_id = (", source)
        self.assertIn("WHERE code = :region_code", source)
        self.assertIn("ORDER BY cell.id", source)

    def test_stage1_pipeline_name_stays_default(self):
        self.assertEqual(fetch_cdse_ndvi.DEFAULT_PIPELINE_NAME, "stage1_cdse_ndvi")
        self.assertIn("--pipeline-name", inspect.getsource(fetch_cdse_ndvi.parse_args))

    def test_no_ndvi_record_uses_supplied_pipeline_name(self):
        record = fetch_cdse_ndvi._no_ndvi_record(
            region_id=1,
            cell_id=2,
            cell_uuid="00000000-0000-0000-0000-000000000002",
            region_code="TR",
            grid_version="grid-v1",
            max_cloud_coverage=40,
            fallback_date=date(2026, 8, 17),
            geometry_pixel_count=None,
            min_valid_pixel_ratio=0.35,
            pipeline_name="stage4_layer_b_ndvi",
        )

        self.assertIn('"pipeline":"stage4_layer_b_ndvi"', record["metadata"])


if __name__ == "__main__":
    unittest.main()
