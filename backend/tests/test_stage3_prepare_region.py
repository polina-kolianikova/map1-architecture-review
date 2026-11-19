import unittest

from app.commands.run_stage3_prepare_region import (
    TILED_COVERAGE_UNION_TILE_SIZE_M,
    bbox_to_wkt,
    parse_bbox,
    parse_source_layers,
    _can_use_staged_source_union,
    _guard_legacy_layer_a_full_scope,
    _guard_legacy_manual_full_scope,
    _upsert_region_from_layer_a_with_mask,
    _manual_source,
    _uses_mescere_forest_class,
    _validate_args,
    _validate_area,
)
from app.services.forest_scope import forest_region_metadata, public_forest_mask_metadata_ready
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_SOURCE_REGION_CODE,
    STAGE3_PUBLIC_V9_REGION_CODE,
)


class Stage3PrepareRegionTest(unittest.TestCase):
    def test_legacy_manual_prepare_cannot_write_final_v10_mask(self):
        with self.assertRaisesRegex(RuntimeError, "cannot write the final Turkey V10 client mask"):
            _guard_legacy_manual_full_scope(
                approved=True,
                region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                scope_area_ha=533,
            )

    def test_legacy_manual_prepare_locks_national_alias_without_approval(self):
        with self.assertRaisesRegex(RuntimeError, "Full Turkey V10 generation is locked"):
            _guard_legacy_manual_full_scope(
                approved=False,
                region_code="TR-TURKEY-ALIAS",
                scope_area_ha=1_250_000,
            )

    def test_legacy_manual_prepare_allows_approved_full_source_seed(self):
        _guard_legacy_manual_full_scope(
            approved=True,
            region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
            scope_area_ha=23_000_000,
        )

    def test_legacy_layer_a_prepare_locks_full_source_without_approval(self):
        with self.assertRaisesRegex(RuntimeError, "Full Turkey V10 generation is locked"):
            _guard_legacy_layer_a_full_scope(
                approved=False,
                region_code="TR-TURKEY-ALIAS",
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                scope_area_ha=None,
            )

    def test_legacy_layer_a_prepare_is_disabled_for_approved_national_scope(self):
        with self.assertRaisesRegex(RuntimeError, "prepare_region --from-layer-a is disabled"):
            _guard_legacy_layer_a_full_scope(
                approved=True,
                region_code="TR-TURKEY-ALIAS",
                source_region_code="TR-SOURCE-ALIAS",
                scope_area_ha=1_250_000,
            )

    def test_staged_union_tile_size_limits_full_public_memory_pressure(self):
        self.assertEqual(TILED_COVERAGE_UNION_TILE_SIZE_M, 10_000)

    def test_parse_bbox_requires_ordered_coordinates(self):
        self.assertEqual(parse_bbox("25,35.5,45.2,42.5"), (25.0, 35.5, 45.2, 42.5))

        with self.assertRaisesRegex(ValueError, "longitude"):
            parse_bbox("45,35,25,42")
        with self.assertRaisesRegex(ValueError, "latitude"):
            parse_bbox("25,43,45,35")

    def test_bbox_to_wkt_closes_polygon(self):
        self.assertEqual(
            bbox_to_wkt((1, 2, 3, 4)),
            "POLYGON((1 2,3 2,3 4,1 4,1 2))",
        )

    def test_area_guard_rejects_wrong_scale(self):
        with self.assertRaisesRegex(RuntimeError, "below required minimum"):
            _validate_area(528, min_area_ha=20_000_000, max_area_ha=0)

        with self.assertRaisesRegex(RuntimeError, "above required maximum"):
            _validate_area(80_000_000, min_area_ha=0, max_area_ha=30_000_000)

    def test_manual_source_is_recorded_for_seed_regions(self):
        class Args:
            wkt_file = ""
            turkey_bbox = True

        self.assertEqual(_manual_source(Args()), "turkey_bbox")

    def test_clip_to_grid_version_requires_layer_a_mode(self):
        class Args:
            bbox = "1,2,3,4"
            turkey_bbox = False
            wkt_file = ""
            from_layer_a = False
            min_area_ha = 0
            max_area_ha = 0
            projected_srid = 32636
            clip_to_grid_version = "stage1-test-v1"
            intersect_mask_source_layers = ""

        with self.assertRaisesRegex(ValueError, "clip-to-grid-version"):
            _validate_args(Args())

    def test_intersect_mask_requires_layer_a_mode(self):
        class Args:
            bbox = "1,2,3,4"
            turkey_bbox = False
            wkt_file = ""
            from_layer_a = False
            source_layers = ""
            min_area_ha = 0
            max_area_ha = 0
            projected_srid = 32636
            clip_to_grid_version = ""
            intersect_mask_source_layers = "osm_forest_mask"
            difference_mask_source_layers = ""

        with self.assertRaisesRegex(ValueError, "intersect-mask-source-layers"):
            _validate_args(Args())

    def test_source_layers_require_layer_a_mode(self):
        class Args:
            bbox = "1,2,3,4"
            turkey_bbox = False
            wkt_file = ""
            from_layer_a = False
            source_layers = "osm_forest_mask"
            min_area_ha = 0
            max_area_ha = 0
            projected_srid = 32636
            clip_to_grid_version = ""
            intersect_mask_source_layers = ""
            difference_mask_source_layers = ""

        with self.assertRaisesRegex(ValueError, "source-layers"):
            _validate_args(Args())

    def test_intersect_mask_cannot_use_grid_clip_shortcut(self):
        class Args:
            bbox = ""
            turkey_bbox = False
            wkt_file = ""
            from_layer_a = True
            min_area_ha = 0
            max_area_ha = 0
            projected_srid = 32636
            clip_to_grid_version = "stage1-test-v1"
            intersect_mask_source_layers = "osm_forest_mask"
            difference_mask_source_layers = ""

        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            _validate_args(Args())

    def test_parse_source_layers_ignores_empty_values(self):
        self.assertEqual(parse_source_layers(" ogm_esatis_bolme, ,osm_forest_mask "), ["ogm_esatis_bolme", "osm_forest_mask"])

    def test_mescere_forest_class_only_applies_when_mescere_is_primary_source(self):
        self.assertFalse(_uses_mescere_forest_class(["osm_forest_mask"]))
        self.assertTrue(_uses_mescere_forest_class(["osm_forest_mask", "ogm_yayin_mescere_wms_svg"]))

    def test_simple_public_mask_can_use_staged_union(self):
        self.assertTrue(
            _can_use_staged_source_union(
                source_layers=["osm_forest_mask"],
                intersect_source_layers=[],
                difference_source_layers=[],
                clip_to_grid_version="",
            )
        )
        self.assertFalse(
            _can_use_staged_source_union(
                source_layers=["osm_forest_mask", "ogm_yayin_mescere_wms_svg"],
                intersect_source_layers=[],
                difference_source_layers=[],
                clip_to_grid_version="",
            )
        )
        self.assertTrue(
            _can_use_staged_source_union(
                source_layers=["osm_forest_mask"],
                intersect_source_layers=[],
                difference_source_layers=["osm_nonforest_exclusion"],
                clip_to_grid_version="",
            )
        )

    def test_simple_public_mask_prepare_uses_staged_union(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def scalar_one(self):
                return self.value

            def mappings(self):
                return self

            def one_or_none(self):
                return self.value

        class Db:
            def __init__(self):
                self.statements = []
                self.counts = [3, 1]

            def execute(self, statement, params=None):
                sql = str(statement)
                self.statements.append((sql, params or {}))
                if "SELECT count(*)" in sql:
                    return Result(self.counts.pop(0))
                if "FROM changed" in sql:
                    return Result(
                        {
                            "code": "TR-FOREST",
                            "name": "Turkey Forest",
                            "country_code": "TR",
                            "area_ha": 12_345.0,
                            "source": "forest_mask_layer_a",
                        }
                    )
                return Result(None)

        db = Db()
        result = _upsert_region_from_layer_a_with_mask(
            db,
            region_code="TR-FOREST",
            region_name="Turkey Forest",
            country_code="TR",
            source_region_code="TR-STAGE3-PUBLIC-FULL",
            source_layers=["osm_forest_mask"],
            intersect_source_layers=[],
            difference_source_layers=[],
            clip_to_grid_version="",
            projected_srid=6933,
        )

        joined_sql = "\n".join(statement for statement, _ in db.statements)
        self.assertEqual(result["code"], "TR-FOREST")
        self.assertIn("CREATE TEMP TABLE stage3_prepare_region_source_geoms", joined_sql)
        self.assertIn("ST_MakeValid(ST_Intersection(ST_MakeValid(parcel.geom), source_region.geom))", joined_sql)
        self.assertIn("parcel.geom && source_region.geom", joined_sql)
        self.assertNotIn("SELECT ST_Transform(ST_MakeValid(geom)", joined_sql)
        self.assertIn("CREATE INDEX stage3_prepare_region_source_geoms_gix", joined_sql)
        self.assertIn("ANALYZE stage3_prepare_region_source_geoms", joined_sql)
        self.assertIn("CREATE TEMP TABLE stage3_prepare_region_union_work", joined_sql)
        self.assertIn("ST_MakeEnvelope", joined_sql)
        self.assertIn("generate_series(min_i, max_i - 1)", joined_sql)
        self.assertIn("source.geom && tile.geom", joined_sql)
        self.assertIn("ST_Intersects(source.geom, tile.geom)", joined_sql)
        self.assertIn("ST_Intersection(source.geom, tile.geom)", joined_sql)
        self.assertNotIn("intersect.geom", joined_sql)
        self.assertIn("batch_union AS", joined_sql)
        self.assertIn("GROUP BY floor((id - 1) / 100)", joined_sql)
        self.assertIn("raw_union AS", joined_sql)
        self.assertIn("clean_components AS", joined_sql)
        self.assertIn("ST_UnaryUnion(ST_Collect(geom))", joined_sql)
        self.assertIn("ST_Area(component.geom) / 10000.0 >= :min_mask_component_area_ha", joined_sql)
        self.assertNotIn("ST_CoverageUnion(geom)", joined_sql)
        self.assertNotIn("stage3_prepare_region_union_next", joined_sql)
        self.assertNotIn("ST_MemUnion", joined_sql)

    def test_public_mask_prepare_uses_staged_intersection_and_difference(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def scalar_one(self):
                return self.value

            def mappings(self):
                return self

            def one_or_none(self):
                return self.value

        class Db:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=None):
                sql = str(statement)
                self.statements.append((sql, params or {}))
                if "SELECT count(*)" in sql:
                    return Result(1)
                if "FROM changed" in sql:
                    return Result(
                        {
                            "code": "TR-FOREST",
                            "name": "Turkey Forest",
                            "country_code": "TR",
                            "area_ha": 12_345.0,
                            "source": "forest_mask_layer_a",
                        }
                    )
                return Result(None)

        db = Db()
        _upsert_region_from_layer_a_with_mask(
            db,
            region_code="TR-FOREST",
            region_name="Turkey Forest",
            country_code="TR",
            source_region_code="TR-STAGE3-PUBLIC-FULL",
            source_layers=["ogm_esatis_bolme"],
            intersect_source_layers=["osm_forest_mask"],
            difference_source_layers=["osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"],
            clip_to_grid_version="",
            projected_srid=6933,
        )

        joined_sql = "\n".join(statement for statement, _ in db.statements)
        stage_params = next(
            params
            for statement, params in db.statements
            if "CREATE TEMP TABLE stage3_prepare_region_union_work" in statement
        )
        self.assertIn("stage3_prepare_region_intersect_geoms", joined_sql)
        self.assertIn("stage3_prepare_region_difference_geoms", joined_sql)
        self.assertIn("ST_MakeValid(ST_Intersection(ST_MakeValid(parcel.geom), source_region.geom))", joined_sql)
        self.assertIn("parcel.geom && source_region.geom", joined_sql)
        self.assertNotIn("SELECT ST_Transform(ST_MakeValid(geom)", joined_sql)
        self.assertIn("ST_Intersection(source.geom, intersect_mask.geom)", joined_sql)
        self.assertNotIn("intersect.geom", joined_sql)
        self.assertIn("ST_Difference(ST_MakeValid(base.geom), difference_mask.geom)", joined_sql)
        self.assertIn("batch_union AS", joined_sql)
        self.assertIn("GROUP BY floor((id - 1) / 100)", joined_sql)
        self.assertIn("raw_union AS", joined_sql)
        self.assertIn("clean_components AS", joined_sql)
        self.assertTrue(stage_params["use_intersect_mask"])
        self.assertTrue(stage_params["use_difference_mask"])
        self.assertNotIn("ST_MemUnion", joined_sql)

    def test_public_mask_prepare_can_use_source_mask_cache(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def scalar_one(self):
                return self.value

            def mappings(self):
                return self

            def one_or_none(self):
                return self.value

        class Db:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=None):
                sql = str(statement)
                self.statements.append((sql, params or {}))
                if "SELECT count(*)" in sql:
                    return Result(1)
                if "FROM changed" in sql:
                    return Result(
                        {
                            "code": "TR-FOREST",
                            "name": "Turkey Forest",
                            "country_code": "TR",
                            "area_ha": 12_345.0,
                            "source": "forest_mask_layer_a",
                        }
                    )
                return Result(None)

        db = Db()
        _upsert_region_from_layer_a_with_mask(
            db,
            region_code="TR-FOREST",
            region_name="Turkey Forest",
            country_code="TR",
            source_region_code="TR-STAGE3-PUBLIC-FULL",
            source_layers=["osm_forest_mask"],
            intersect_source_layers=[],
            difference_source_layers=["osm_nonforest_exclusion"],
            clip_to_grid_version="",
            projected_srid=6933,
            min_mask_component_area_ha=1.0,
            use_source_mask_cache=True,
        )

        joined_sql = "\n".join(statement for statement, _ in db.statements)
        self.assertIn("FROM stage3_source_mask_cache", joined_sql)
        self.assertIn("ST_MakeValid(ST_Intersection(cache.geom_projected, source_region.geom)) AS geom", joined_sql)
        self.assertIn("cache.geom_projected && source_region.geom", joined_sql)
        self.assertIn("projected_srid = :projected_srid", joined_sql)
        self.assertIn("ST_Area(geom) / 10000.0 >= :min_mask_component_area_ha", joined_sql)
        self.assertIn("ST_MakeValid(ST_SnapToGrid(source.geom, 0.01))", joined_sql)
        self.assertIn("ST_Subdivide(", joined_sql)
        self.assertIn("ST_Multi(ST_CollectionExtract(ST_Collect(geom), 3))", joined_sql)
        self.assertNotIn("FROM ogm_parcels parcel", joined_sql)

    def test_intersect_mask_must_be_auxiliary_forest_source(self):
        with self.assertRaisesRegex(RuntimeError, "auxiliary forest mask"):
            _upsert_region_from_layer_a_with_mask(
                object(),
                region_code="TR-FOREST",
                region_name="Turkey Forest",
                country_code="TR",
                source_region_code="TR-STAGE1-TEST",
                source_layers=None,
                intersect_source_layers=["ogm_esatis_bolme"],
                difference_source_layers=[],
                clip_to_grid_version="",
                projected_srid=32636,
            )

    def test_forest_region_metadata_records_grid_clip(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE1-TEST",
            source_layers=["ogm_esatis_bolme"],
            clip_to_grid_version="stage1-test-v1",
        )

        self.assertTrue(metadata["forest_only"])
        self.assertEqual(metadata["forest_mask"]["clip_to_grid_version"], "stage1-test-v1")
        self.assertEqual(metadata["forest_mask"]["method"], "dissolved_layer_a_forest_class_geometry_clipped_to_grid")

    def test_forest_region_metadata_records_auxiliary_osm_clip(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE1-TEST",
            source_layers=["ogm_esatis_bolme"],
            intersect_source_layers=["osm_forest_mask"],
            difference_source_layers=["osm_nonforest_exclusion"],
            min_mask_component_area_ha=1.0,
        )

        self.assertEqual(
            metadata["forest_mask"]["source"],
            "public_ogm_layer_a_with_osm_forest_clip_and_nonforest_difference",
        )
        self.assertEqual(metadata["forest_mask"]["intersect_source_layers"], ["osm_forest_mask"])
        self.assertEqual(metadata["forest_mask"]["difference_source_layers"], ["osm_nonforest_exclusion"])
        self.assertEqual(
            metadata["forest_mask"]["method"],
            "dissolved_layer_a_forest_class_geometry_with_public_forest_mask_and_nonforest_difference",
        )
        self.assertEqual(metadata["forest_mask"]["clean_mask_policy"]["min_component_area_ha"], 1.0)
        self.assertTrue(metadata["forest_mask"]["clean_mask_policy"]["applies_before_grid_insert"])

    def test_forest_region_metadata_allows_public_osm_only_fallback(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE1-TEST",
            source_layers=["osm_forest_mask"],
            difference_source_layers=["osm_nonforest_exclusion"],
        )

        self.assertEqual(metadata["forest_mask"]["source"], "public_osm_forest_mask_with_nonforest_difference")
        self.assertEqual(
            metadata["forest_mask"]["method"],
            "public_osm_forest_mask_geometry_and_nonforest_difference",
        )
        self.assertTrue(public_forest_mask_metadata_ready(metadata))

    def test_forest_region_metadata_records_public_ogm_osm_union(self):
        metadata = forest_region_metadata(
            source_region_code="TR-STAGE3-PUBLIC-FULL",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            difference_source_layers=["osm_nonforest_exclusion"],
            min_mask_component_area_ha=1.0,
        )

        self.assertEqual(
            metadata["forest_mask"]["source"],
            "public_ogm_layer_a_plus_osm_forest_mask_with_nonforest_difference",
        )
        self.assertEqual(
            metadata["forest_mask"]["method"],
            "public_ogm_layer_a_union_osm_forest_mask_geometry_and_nonforest_difference",
        )
        self.assertEqual(metadata["forest_mask"]["intersect_source_layers"], [])
        self.assertTrue(public_forest_mask_metadata_ready(metadata))


if __name__ == "__main__":
    unittest.main()
