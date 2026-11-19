import unittest

from app.commands.check_stage3_grid_spatial_integrity import grid_spatial_integrity_report


class Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def mappings(self):
        return self

    def one(self):
        return self._one

    def all(self):
        return self._rows


class GridSpatialIntegrityDb:
    def __init__(self, tile_metrics):
        self.sql = []
        self.params = []
        self.tile_metrics = list(tile_metrics)

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        self.params.append(params or {})
        if "latest_run AS" in sql:
            return Result(
                one={
                    "run_id": 7,
                    "region_id": 2,
                    "status": "completed",
                    "total_tiles": 2,
                    "processed_tiles": 2,
                    "inserted_cells": 12,
                    "grid_size_m": 100,
                    "tile_size_m": 10000,
                    "metadata": {"grid_mask_method": "region_mask_components"},
                    "tile_count": 2,
                    "completed_tiles": 2,
                    "incomplete_tiles": 0,
                }
            )
        if "FROM stage3_grid_generation_tiles" in sql and "min_x" in sql:
            return Result(
                rows=[
                    {"id": 10, "tile_key": "1:1", "min_x": 0, "min_y": 0, "max_x": 100, "max_y": 100},
                    {"id": 11, "tile_key": "1:2", "min_x": 0, "min_y": 100, "max_x": 100, "max_y": 200},
                ]
            )
        if "persisted_cells" in sql:
            return Result(one={"persisted_cells": 12, "empty_cells": 0, "invalid_cells": 0})
        return Result(one=self.tile_metrics.pop(0))


def tile_metric(**overrides):
    row = {
        "cell_count": 6,
        "empty_cells": 0,
        "invalid_cells": 0,
        "cell_overlap_m2": 0,
        "mask_without_cells_m2": 0,
        "cells_outside_mask_m2": 0,
    }
    row.update(overrides)
    return row


class Stage3GridSpatialIntegrityTest(unittest.TestCase):
    def test_report_is_ready_when_all_tiles_match_mask(self):
        report = grid_spatial_integrity_report(
            GridSpatialIntegrityDb([tile_metric(), tile_metric()]),
            region_code="TR-V8",
            grid_version="grid-v8",
            mask_version="TR-V8",
            projected_srid=6933,
        )

        self.assertTrue(report["checks"]["ready"])
        self.assertEqual(report["checked_cells"], 12)
        self.assertEqual(report["persisted_cells"], 12)
        self.assertTrue(report["checks"]["grid_size_is_100m"])
        self.assertTrue(report["checks"]["tile_queue_count_matches_run_total"])

    def test_report_rejects_tile_gap_outside_and_overlap(self):
        report = grid_spatial_integrity_report(
            GridSpatialIntegrityDb(
                [
                    tile_metric(mask_without_cells_m2=2.0, cells_outside_mask_m2=0, cell_overlap_m2=0),
                    tile_metric(mask_without_cells_m2=0, cells_outside_mask_m2=3.0, cell_overlap_m2=4.0),
                ]
            ),
            region_code="TR-V8",
            grid_version="grid-v8",
            mask_version="TR-V8",
            projected_srid=6933,
            max_tile_gap_m2=1.0,
            max_total_gap_m2=1.0,
        )

        self.assertFalse(report["checks"]["no_mask_gap"])
        self.assertFalse(report["checks"]["no_cells_outside_mask"])
        self.assertFalse(report["checks"]["no_positive_cell_overlap"])
        self.assertEqual(report["tiles_with_gap"], 1)
        self.assertEqual(report["tiles_with_outside"], 1)
        self.assertEqual(report["tiles_with_overlap"], 1)

    def test_tile_query_compares_component_mask_and_cell_union(self):
        db = GridSpatialIntegrityDb([tile_metric(), tile_metric()])

        grid_spatial_integrity_report(
            db,
            region_code="TR-V8",
            grid_version="grid-v8",
            mask_version="TR-V8",
            projected_srid=6933,
        )

        tile_sql = db.sql[-1]
        self.assertIn("stage3_region_mask_components", tile_sql)
        self.assertIn("grid_cells", tile_sql)
        self.assertIn("ST_Intersection(", tile_sql)
        self.assertIn("ST_CollectionExtract", tile_sql)
        self.assertIn("tile.geom_projected", tile_sql)
        self.assertIn("ST_Difference(mask.geom, cell_union.geom)", tile_sql)
        self.assertIn("cell_metrics.cell_area_m2 - COALESCE(ST_Area(cell_union.geom), 0)", tile_sql)

    def test_run_query_loads_grid_size_and_tile_size_for_acceptance_gate(self):
        db = GridSpatialIntegrityDb([tile_metric(), tile_metric()])

        grid_spatial_integrity_report(
            db,
            region_code="TR-V8",
            grid_version="grid-v8",
            mask_version="TR-V8",
            projected_srid=6933,
        )

        run_sql = db.sql[0]
        self.assertIn("latest_run.grid_size_m", run_sql)
        self.assertIn("latest_run.tile_size_m", run_sql)


if __name__ == "__main__":
    unittest.main()
