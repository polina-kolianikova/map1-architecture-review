import unittest
import inspect
from unittest.mock import patch

from app.commands.ingest_worldcover_nonforest_exclusion import (
    NONFOREST_CLASSES,
    WORLDCOVER_VECTOR_RESOLUTION_M,
    bbox_to_wkt,
    intersect_bbox,
    parse_bbox,
    quote_identifier,
    quote_sql_literal,
    staging_table_name,
    store_worldcover_exclusions,
    worldcover_nonforest_calc_expression,
    worldcover_tile_bbox,
    worldcover_tile_name,
    worldcover_tiles,
)


class WorldCoverNonForestExclusionTest(unittest.TestCase):
    def test_selects_expected_worldcover_tiles(self):
        self.assertEqual(worldcover_tiles((31.57, 40.70, 31.59, 40.72)), ["N39E030"])
        self.assertEqual(
            worldcover_tiles((32.99, 40.0, 33.01, 40.1)),
            ["N39E030", "N39E033"],
        )

    def test_tile_names_cover_both_hemispheres(self):
        self.assertEqual(worldcover_tile_name(39, 30), "N39E030")
        self.assertEqual(worldcover_tile_name(-3, -6), "S03W006")

    def test_tile_bbox_is_used_to_crop_full_region_work(self):
        self.assertEqual(worldcover_tile_bbox("N39E030"), (30.0, 39.0, 33.0, 42.0))
        self.assertEqual(
            intersect_bbox((25.0, 35.5, 45.2, 42.5), worldcover_tile_bbox("N39E030")),
            (30.0, 39.0, 33.0, 42.0),
        )
        self.assertEqual(
            intersect_bbox((31.57, 40.70, 31.59, 40.72), worldcover_tile_bbox("N39E030")),
            (31.57, 40.70, 31.59, 40.72),
        )
        self.assertIsNone(intersect_bbox((31.0, 40.0, 32.0, 41.0), worldcover_tile_bbox("N36E033")))

    def test_nonforest_classes_include_grassland_cropland_buildings_bare_land_and_water(self):
        self.assertEqual(NONFOREST_CLASSES[30], "grassland")
        self.assertEqual(NONFOREST_CLASSES[40], "cropland")
        self.assertEqual(NONFOREST_CLASSES[50], "built_up")
        self.assertEqual(NONFOREST_CLASSES[60], "bare_sparse_vegetation")
        self.assertEqual(NONFOREST_CLASSES[80], "permanent_water")
        self.assertNotIn(10, NONFOREST_CLASSES)
        self.assertNotIn(20, NONFOREST_CLASSES)

    def test_worldcover_mask_is_vectorized_as_binary_nonforest(self):
        expression = worldcover_nonforest_calc_expression()

        self.assertIn("A==30", expression)
        self.assertIn("A==80", expression)
        self.assertTrue(expression.startswith("where("))
        self.assertTrue(expression.endswith(",1,0)"))
        self.assertEqual(WORLDCOVER_VECTOR_RESOLUTION_M, 100)

    def test_staging_identifier_is_safe(self):
        self.assertEqual(
            staging_table_name("TR-STAGE3 PUBLIC", 2),
            "stage3_worldcover_nonforest_tr_stage3_public_2",
        )
        self.assertEqual(
            staging_table_name("TR-STAGE3 PUBLIC", 2, suffix="153010"),
            "stage3_worldcover_nonforest_tr_stage3_public_153010_2",
        )
        self.assertEqual(quote_identifier("safe_table_2"), '"safe_table_2"')
        with self.assertRaisesRegex(ValueError, "unsafe SQL identifier"):
            quote_identifier("unsafe;drop")
        self.assertEqual(quote_sql_literal("N36E027"), "'N36E027'")
        with self.assertRaisesRegex(ValueError, "unsafe SQL literal"):
            quote_sql_literal("N36E027';drop")

    def test_bbox_parser_validates_worldcover_clip_extent(self):
        bbox = parse_bbox("31.57,40.70,31.59,40.72")
        self.assertEqual(bbox, (31.57, 40.70, 31.59, 40.72))
        self.assertEqual(
            bbox_to_wkt(bbox),
            "POLYGON((31.57 40.7,31.59 40.7,31.59 40.72,31.57 40.72,31.57 40.7))",
        )
        with self.assertRaisesRegex(ValueError, "bbox longitude range is invalid"):
            parse_bbox("31.59,40.70,31.57,40.72")

    def test_connection_string_does_not_include_password(self):
        from app.commands.ingest_worldcover_nonforest_exclusion import _ogr_pg_connection

        with patch("app.core.config.get_settings") as get_settings:
            get_settings.return_value.postgres_host = "db"
            get_settings.return_value.postgres_port = 5432
            get_settings.return_value.postgres_db = "map1"
            get_settings.return_value.postgres_user = "map1_owner"
            get_settings.return_value.postgres_password = "must-not-appear"
            connection = _ogr_pg_connection()
        self.assertNotIn("must-not-appear", connection)
        self.assertNotIn("password=", connection)

    def test_ogr_environment_allows_large_worldcover_geojson(self):
        from app.commands.ingest_worldcover_nonforest_exclusion import _ogr_environment

        with patch("app.core.config.get_settings") as get_settings:
            get_settings.return_value.postgres_password = "secret"
            environment = _ogr_environment()

        self.assertEqual(environment["PGPASSWORD"], "secret")
        self.assertEqual(environment["OGR_GEOJSON_MAX_OBJ_SIZE"], "0")

    def test_store_compacts_worldcover_per_tile_without_coverage_union(self):
        from app.commands.ingest_worldcover_nonforest_exclusion import _insert_worldcover_tile_exclusions

        code = inspect.getsource(_insert_worldcover_tile_exclusions)

        self.assertNotIn("ST_CoverageUnion", code)
        self.assertIn("ST_UnaryUnion(ST_Collect(geom))", code)
        self.assertIn("ST_Dump", code)
        self.assertIn("GROUP BY tile_index, tile_name, class_code", code)
        self.assertIn("source_row_id", code)

    def test_worldcover_store_keeps_compacted_exclusion_components(self):
        class FakeResult:
            def scalar_one(self):
                return []

        class FakeDb:
            def __init__(self):
                self.statements = []

            def execute(self, statement, _params):
                self.statements.append(str(statement))
                return FakeResult()

            def commit(self):
                return None

            def rollback(self):
                return None

        db = FakeDb()
        store_worldcover_exclusions(
            db,
            region={
                "id": 1,
                "code": "TR-STAGE3-PUBLIC-FULL",
                "clip_wkt": bbox_to_wkt((25.0, 35.5, 45.2, 42.5)),
                "bbox": [25.0, 35.5, 45.2, 42.5],
                "clip_source": "bbox",
            },
            staging_tables=["stage3_worldcover_nonforest_tr_stage3_public_full_123456_0"],
            started_at="2026-07-30T00:00:00+00:00",
            imported_rows=1,
            projected_srid=6933,
            min_area_ha=0.001,
            buffer_m=0.0,
            tiles=["N33E024"],
            requested_tiles=["N33E024"],
            skipped_tiles=[],
        )

        statements = "\n".join(db.statements)
        self.assertIn("source_row_id", statements)
        self.assertIn("ST_MakeValid(source.geom)", statements)
        self.assertIn("ST_MakeValid(component.geom)", statements)
        self.assertIn("worldcover:2021:v200:", statements)
        self.assertNotIn("ST_CoverageUnion", statements)
        self.assertIn("ST_UnaryUnion(ST_Collect(geom))", statements)
        self.assertIn("ST_Dump", statements)


if __name__ == "__main__":
    unittest.main()
