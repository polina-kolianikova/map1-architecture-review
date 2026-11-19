import json
import re
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.api import stage1


class SqlResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)

    def one(self):
        if len(self.rows) != 1:
            raise AssertionError(f"Expected one result, got {len(self.rows)}")
        return self.rows[0]

    def scalar_one_or_none(self):
        return next(iter(self.rows[0].values())) if self.rows else None


class SqliteSession:
    """Run emitted API SQL locally, adapting only PostgreSQL syntax.

    The unrelated Layer A freshness query is stubbed; summary counts, scope
    resolution, verification status counts, and observation listings execute.
    """

    def __init__(self, connection):
        self.connection = connection
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "WITH configured_sources AS" in sql:
            return SqlResult([])
        sql = sql.replace("source_layer = ANY(:layer_a_source_layers)",
                          "source_layer IN (SELECT value FROM json_each(:layer_a_source_layers))")
        sql = re.sub(r"\((\w+\.metadata->>'[^']+')\)::(numeric|integer)", r"CAST(\1 AS \2)", sql)
        sql = sql.replace("cell.uuid::text", "CAST(cell.uuid AS text)")
        sql = sql.replace("'[]'::jsonb", "'[]'").replace(" ~ ", " REGEXP ")
        bindings = {key: json.dumps(value) if isinstance(value, list) else value
                    for key, value in (params or {}).items()}
        return SqlResult([dict(row) for row in self.connection.execute(sql, bindings)])


class Stage1ObservationSourcesTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function("greatest", -1, max)
        self.connection.create_function("regexp", 2, lambda pattern, value: bool(re.search(pattern, str(value))))
        self.connection.executescript("""
            CREATE TABLE regions (id INTEGER PRIMARY KEY, code TEXT, metadata TEXT);
            CREATE TABLE grid_cells (id INTEGER PRIMARY KEY, uuid TEXT, region_id INTEGER,
                                     grid_version TEXT, area_ha REAL);
            CREATE TABLE stage3_grid_generation_runs (id INTEGER PRIMARY KEY, region_id INTEGER,
                grid_version TEXT, status TEXT, inserted_cells INTEGER, finished_at TEXT);
            CREATE TABLE ogm_parcels (id INTEGER, is_current BOOLEAN, source_layer TEXT, attributes TEXT);
            CREATE TABLE verification_results (id INTEGER, grid_cell_id INTEGER, is_current BOOLEAN,
                region_id INTEGER, region_code TEXT, grid_version TEXT, metadata TEXT, anomaly_type TEXT);
            CREATE TABLE verification_audit_events (id INTEGER, region_id INTEGER,
                region_code TEXT, grid_version TEXT, metadata TEXT);
        """)
        for table in ("sentinel_observations", "sentinel_observations_partitioned"):
            self.connection.execute(f"""CREATE TABLE {table} (
                id INTEGER PRIMARY KEY, grid_cell_id INTEGER, region_id INTEGER,
                region_code TEXT, grid_version TEXT, acquisition_date TEXT, ndvi REAL,
                cloud_coverage REAL, processing_level TEXT, metadata TEXT, is_current BOOLEAN
            )""")
        self.connection.executemany("INSERT INTO regions VALUES (?, ?, ?)", [
            (1, "TR-PILOT", json.dumps({"forest_mask": {"source_layers": ["ogm_esatis_bolme"]}})),
            (2, "TR-OTHER", "{}"),
        ])
        self.session = SqliteSession(self.connection)
        self.addCleanup(patch.stopall)
        patch.object(stage1, "SessionLocal", return_value=self.session).start()
        patch.object(stage1, "verification_results_table_sql", return_value="verification_results").start()

    def add_cell(self, cell_id, region_id=1, grid_version="pilot-v1"):
        self.connection.execute("INSERT INTO grid_cells VALUES (?, ?, ?, ?, 1)",
                                (cell_id, f"cell-{cell_id}", region_id, grid_version))

    def add_observation(self, observation_id, *, metadata=None, **overrides):
        values = {
            "id": observation_id, "grid_cell_id": observation_id, "region_id": 1,
            "region_code": "TR-PILOT", "grid_version": "pilot-v1",
            "acquisition_date": "2026-11-01", "ndvi": 0.6, "is_current": True,
            **overrides,
            "metadata": json.dumps({
                "pipeline": "stage4_layer_b_ndvi", "statistics_source": "public_raster_zonal",
                "status": "ok", "sample_count": 10, "valid_pixel_count": 10, "valid_pixel_ratio": 1,
                **(metadata or {}),
            }),
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        # Each selected read surface gets its own evidence, without unioning a
        # partition mirror with the base table and counting observations twice.
        for table in ("sentinel_observations", "sentinel_observations_partitioned"):
            self.connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)

    def summary(self):
        return stage1.get_summary(region_code="TR-PILOT", grid_version="pilot-v1")

    def observations(self, limit=200, offset=0):
        return stage1.list_observations(limit=limit, offset=offset, region_code="TR-PILOT", grid_version="pilot-v1")

    def test_public_cohort_counts_all_records_and_only_measured_ok_values(self):
        for cell_id in range(1, 3111):
            self.add_cell(cell_id)
            missing = cell_id > 3000
            self.add_observation(cell_id, ndvi=None if missing else 0.6, metadata={
                "status": "no_sample_pixels" if missing else "ok",
                "sample_count": 0 if missing else 10,
                "valid_pixel_count": 0 if missing else 10,
                "valid_pixel_ratio": 0 if missing else 1,
            })
        # Exercise the existing completed-grid count path as well.
        self.connection.execute("INSERT INTO stage3_grid_generation_runs VALUES (1,1,'pilot-v1','completed',3110,'2026-11-01')")
        for table in ("sentinel_observations", "sentinel_observations_partitioned"):
            with self.subTest(table=table), patch.object(stage1, "sentinel_observations_table_sql", return_value=table):
                result = self.summary()
                self.assertEqual(result["sentinel_observations"], 3110)
                self.assertEqual(result["real_sentinel_observations"], 3000)
                self.assertEqual(result["ndvi_coverage_ratio"], round(3000 / 3110, 5))
                missing_rows = self.observations(offset=3000)
                self.assertEqual(len(missing_rows), 110)
                self.assertTrue(all(row["ndvi"] is None and row["status"] == "no_sample_pixels" for row in missing_rows))
                self.assertTrue(all(row["sample_count"] == row["valid_pixel_count"] == row["valid_pixel_ratio"] == 0 for row in missing_rows))

    def test_all_three_source_pairs_are_supported_and_unrelated_pairs_are_excluded(self):
        pairs = [
            ("stage1_cdse_ndvi", "sentinel_hub_statistical_api"),
            ("stage4_layer_b_ndvi", "sentinel_hub_statistical_api"),
            ("stage4_layer_b_ndvi", "public_raster_zonal"),
            ("stage1_cdse_ndvi", "public_raster_zonal"),
            ("unrelated", "sentinel_hub_statistical_api"),
            ("stage4_layer_b_ndvi", "synthetic"),
            ("stage4_layer_b_ndvi", None),
            (None, "public_raster_zonal"),
        ]
        for cell_id, (pipeline, source) in enumerate(pairs, start=1):
            self.add_cell(cell_id)
            self.add_observation(cell_id, metadata={"pipeline": pipeline, "statistics_source": source})
        result = self.summary()
        self.assertEqual(result["sentinel_observations"], 3)
        self.assertEqual(result["real_sentinel_observations"], 3)
        self.assertEqual([row["id"] for row in self.observations()], [1, 2, 3])

    def test_legacy_total_and_real_count_semantics_remain_compatible(self):
        for cell_id, (ndvi, status, ratio) in enumerate([
            (0.5, None, 1), (None, "request_failed", 0),
            (0.6, "no_valid_pixels", 0), (0.5, "ok", 0.1),
        ], start=1):
            self.add_cell(cell_id)
            self.add_observation(cell_id, ndvi=ndvi, metadata={
                "pipeline": "stage1_cdse_ndvi", "statistics_source": "sentinel_hub_statistical_api",
                "status": status, "valid_pixel_ratio": ratio,
            })
        result = self.summary()
        self.assertEqual(result["sentinel_observations"], 4)
        self.assertEqual(result["real_sentinel_observations"], 2)
        self.assertEqual(result["ndvi_coverage_ratio"], 0.5)
        self.assertEqual(len(self.observations()), 4)

    def test_latest_current_row_is_selected_once_across_pipelines_without_status_fallback(self):
        self.add_cell(1)
        self.add_observation(1, grid_cell_id=1, acquisition_date="2026-10-01", metadata={
            "pipeline": "stage1_cdse_ndvi", "statistics_source": "sentinel_hub_statistical_api",
        })
        self.add_observation(2, grid_cell_id=1, ndvi=None, metadata={"status": "no_sample_pixels"})
        self.add_observation(3, grid_cell_id=1, acquisition_date="2026-11-02", is_current=False)
        result = self.summary()
        self.assertEqual(result["sentinel_observations"], 1)
        self.assertEqual(result["real_sentinel_observations"], 0)
        self.assertEqual([row["id"] for row in self.observations()], [2])

    def test_equal_acquisition_dates_use_id_and_listing_preserves_pagination(self):
        self.add_cell(1)
        self.add_cell(2)
        self.add_observation(1, grid_cell_id=1)
        self.add_observation(2, grid_cell_id=1, metadata={
            "pipeline": "stage1_cdse_ndvi", "statistics_source": "sentinel_hub_statistical_api",
        })
        self.add_observation(3, grid_cell_id=2)
        self.assertEqual(self.summary()["sentinel_observations"], 2)
        self.assertEqual([row["id"] for row in self.observations(limit=1)], [2])
        self.assertEqual([row["id"] for row in self.observations(limit=1, offset=1)], [3])

    def test_wrong_observation_and_grid_cell_scopes_and_retired_rows_are_excluded(self):
        self.add_cell(1)
        self.add_cell(2, region_id=2)
        self.add_cell(3, grid_version="other-v1")
        self.add_observation(1)
        for observation_id, overrides in enumerate([
            {"region_id": 2}, {"region_code": "TR-OTHER"}, {"grid_version": "other-v1"},
            {"grid_cell_id": 2}, {"grid_cell_id": 3}, {"grid_cell_id": 999}, {"is_current": False},
        ], start=10):
            self.add_observation(observation_id, grid_cell_id=overrides.get("grid_cell_id", 1),
                                 **{key: value for key, value in overrides.items() if key != "grid_cell_id"})
        result = self.summary()
        self.assertEqual(result["sentinel_observations"], 1)
        self.assertEqual(result["real_sentinel_observations"], 1)
        self.assertEqual([row["id"] for row in self.observations()], [1])

    def test_history_without_current_observation_does_not_create_coverage(self):
        self.add_cell(1)
        self.add_observation(1, is_current=False)
        self.assertEqual(self.summary()["sentinel_observations"], 0)
        self.assertEqual(self.summary()["real_sentinel_observations"], 0)
        self.assertEqual(self.observations(), [])

    def test_canonical_summary_and_observations_follow_selected_grid_store(self):
        from app.services import stage3_partitioned_reads

        region = stage3_partitioned_reads.V10_REGION_CODE
        grid = stage3_partitioned_reads.V10_GRID_VERSION
        for cell_id in (1, 2, 3):
            self.add_cell(cell_id)
            self.add_observation(cell_id)
        self.connection.execute("UPDATE regions SET code=? WHERE id=1", (region,))
        self.connection.execute("UPDATE grid_cells SET grid_version=?", (grid,))
        for table in ("sentinel_observations", "sentinel_observations_partitioned"):
            self.connection.execute(f"UPDATE {table} SET region_code=?, grid_version=?", (region, grid))
        # A deliberately incomplete shadow copy makes a legacy-grid fallback
        # observable even when both stores contain valid scoped observations.
        self.connection.execute("CREATE TABLE grid_cells_partitioned AS SELECT * FROM grid_cells WHERE id=1")
        for partitioned, expected in ((False, 3), (True, 1)):
            with self.subTest(partitioned=partitioned), patch.object(
                stage3_partitioned_reads, "get_settings", return_value=SimpleNamespace(
                    stage3_v10_partitioned_grid_reads=partitioned,
                    stage3_partitioned_evidence_reads=False,
                )
            ):
                summary = stage1.get_summary(region_code=region, grid_version=grid)
                observations = stage1.list_observations(
                    limit=200, offset=0, region_code=region, grid_version=grid
                )
                self.assertEqual(summary["grid_cells"], expected)
                self.assertEqual(summary["sentinel_observations"], expected)
                self.assertEqual([row["id"] for row in observations], list(range(1, expected + 1)))


if __name__ == "__main__":
    unittest.main()
