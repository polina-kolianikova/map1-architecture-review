import unittest

from app.commands.fetch_cdse_ndvi import load_grid_cells, load_region_wkt, should_retire_current_observations, store_observations


class FakeResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class FakeDb:
    def __init__(self):
        self.inserted = []

    def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT id FROM regions" in sql:
            return FakeResult(1)
        if "INSERT INTO sentinel_observations" in sql:
            self.inserted.append(params)
        return FakeResult(0)


class FakeRows:
    def mappings(self):
        return [
            {"id": 7, "uuid": "selected-cell", "geometry": {"type": "Polygon", "coordinates": []}},
        ]


class RecordingDb:
    def __init__(self):
        self.params = None
        self.statement = None

    def execute(self, statement, params=None):
        self.statement = str(statement)
        self.params = params
        return FakeRows()


class RegionWktDb:
    def __init__(self):
        self.statement = None
        self.params = None

    def execute(self, statement, params=None):
        self.statement = str(statement)
        self.params = params
        return FakeResult("POLYGON((30 40,31 40,31 41,30 41,30 40))")


def ndvi_statistics():
    return {
        "data": [
            {
                "interval": {"from": "2026-01-10T00:00:00Z"},
                "outputs": {
                    "ndvi": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "mean": 0.42,
                                    "sampleCount": 100,
                                    "noDataCount": 5,
                                }
                            }
                        }
                    }
                },
            },
            {
                "interval": {"from": "2026-06-10T00:00:00Z"},
                "outputs": {
                    "ndvi": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "mean": 0.55,
                                    "sampleCount": 100,
                                    "noDataCount": 2,
                                }
                            }
                        }
                    }
                },
            },
        ],
        "geometryPixelCount": 100,
    }


class CdseNdviStorageTest(unittest.TestCase):
    def test_current_observations_are_retired_only_for_full_refresh(self):
        self.assertTrue(should_retire_current_observations(None, ""))
        self.assertFalse(should_retire_current_observations(100, ""))
        self.assertFalse(should_retire_current_observations(None, "selected-cell"))

    def test_load_grid_cells_can_filter_one_cell_uuid_for_retry(self):
        db = RecordingDb()

        cells = load_grid_cells(
            db,
            limit=None,
            region_code="TR-BOLU",
            grid_version="stage3-public-v1",
            cell_uuid="selected-cell",
        )

        self.assertEqual(cells[0]["uuid"], "selected-cell")
        self.assertEqual(db.params["cell_uuid"], "selected-cell")

    def test_load_grid_cells_accepts_batch_offset(self):
        db = RecordingDb()

        cells = load_grid_cells(
            db,
            limit=500,
            region_code="TR-BOLU",
            grid_version="stage3-public-v1",
            offset=1000,
        )

        self.assertEqual(cells[0]["uuid"], "selected-cell")
        self.assertIn("OFFSET :offset", db.statement)
        self.assertEqual(db.params["limit"], 500)
        self.assertEqual(db.params["offset"], 1000)

    def test_catalogue_region_wkt_uses_envelope(self):
        db = RegionWktDb()

        wkt = load_region_wkt(db, "TR-BOLU")

        self.assertEqual(wkt, "POLYGON((30 40,31 40,31 41,30 41,30 40))")
        self.assertIn("ST_Envelope(geom)", db.statement)
        self.assertEqual(db.params["region_code"], "TR-BOLU")

    def test_default_mode_keeps_only_latest_current_observation(self):
        db = FakeDb()

        inserted = store_observations(
            db,
            products=[],
            statistics=ndvi_statistics(),
            cell_id=7,
            cell_uuid="cell-7",
            region_code="TR-BOLU",
            grid_version="stage3-public-v1",
            max_cloud_coverage=40,
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(len(db.inserted), 1)
        self.assertEqual(db.inserted[0]["acquisition_date"].isoformat(), "2026-06-10")
        self.assertTrue(db.inserted[0]["is_current"])

    def test_history_mode_keeps_multiple_non_current_observations(self):
        db = FakeDb()

        inserted = store_observations(
            db,
            products=[],
            statistics=ndvi_statistics(),
            cell_id=7,
            cell_uuid="cell-7",
            region_code="TR-BOLU",
            grid_version="stage3-public-v1",
            max_cloud_coverage=40,
            history_mode=True,
        )

        self.assertEqual(inserted, 2)
        self.assertEqual([row["acquisition_date"].isoformat() for row in db.inserted], ["2026-01-10", "2026-06-10"])
        self.assertEqual([row["is_current"] for row in db.inserted], [False, False])


if __name__ == "__main__":
    unittest.main()
