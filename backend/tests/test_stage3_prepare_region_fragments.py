import unittest

from app.commands.run_stage3_prepare_region_fragments import (
    _build_connected_components,
    _fragment_region_metadata,
    _stage_tiles_and_pieces,
)
from app.services.stage3_public_v9 import (
    STAGE3_PUBLIC_SOURCE_REGION_CODE,
    STAGE3_PUBLIC_V9_GRID_VERSION,
    STAGE3_PUBLIC_V9_REGION_CODE,
    require_stage3_public_v9_full_formula,
    require_stage3_public_v9_full_grid_args,
    require_stage3_public_v9_full_source_cache_args,
    require_user_approved_full_turkey,
)


class RecordingDb:
    def __init__(self):
        self.sql = []
        self.params = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        self.params.append(params or {})
        return self


class MappingRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class AttributeRow(dict):
    def __getattr__(self, name):
        return self[name]


class SameTileComponentDb:
    def __init__(self):
        self.sql = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if "SELECT id, area_m2" in sql and "FROM stage3_fragment_pieces" in sql:
            return MappingRows(
                [
                    AttributeRow(id=1, area_m2=6000.0),
                    AttributeRow(id=2, area_m2=6000.0),
                ]
            )
        if "SELECT left_piece.id AS left_id" in sql:
            if "left_piece.i <> right_piece.i" in sql:
                return []
            return [AttributeRow(left_id=1, right_id=2)]
        return MappingRows([])


class Stage3PrepareRegionFragmentsTest(unittest.TestCase):
    def test_fragment_metadata_records_client_union_formula(self):
        metadata = _fragment_region_metadata(
            region_code="TR-V9",
            source_region_code="TR-SOURCE",
            clip_region_code="TR-SOURCE",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            intersect_layers=[],
            difference_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            tile_size_m=10_000,
            min_mask_component_area_ha=1.0,
            edge_tolerance_m=0.02,
            snap_grid_m=0.01,
        )

        forest_mask = metadata["forest_mask"]
        self.assertEqual(forest_mask["source_layers"], ["ogm_esatis_bolme", "osm_forest_mask"])
        self.assertEqual(forest_mask["intersect_source_layers"], [])
        self.assertEqual(forest_mask["difference_source_layers"], ["osm_nonforest_exclusion"])
        self.assertEqual(
            forest_mask["method"],
            "public_ogm_layer_a_union_osm_forest_mask_geometry_and_nonforest_difference",
        )
        self.assertEqual(forest_mask["source_mask_cache"]["source_layers"], [
            "ogm_esatis_bolme",
            "osm_forest_mask",
            "osm_nonforest_exclusion",
        ])

    def test_stage_tiles_refines_by_intersection_before_difference(self):
        db = RecordingDb()

        _stage_tiles_and_pieces(
            db,
            source_region_code="TR-SOURCE",
            clip_region_code="TR-SOURCE",
            source_layers=["ogm_esatis_bolme"],
            intersect_layers=["osm_forest_mask"],
            difference_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            tile_size_m=10_000,
            snap_grid_m=0.01,
            subdivide_max_vertices=256,
        )

        combined_sql = "\n".join(db.sql)
        self.assertIn("stage3_fragment_intersect_tiles", combined_sql)
        self.assertIn("LEFT JOIN stage3_fragment_intersect_tiles intersect_mask", combined_sql)
        self.assertIn("ST_Intersection(forest.geom, intersect_mask.geom)", combined_sql)
        self.assertIn("ST_Difference(", combined_sql)
        self.assertNotIn("THEN forest.geom", combined_sql)

    def test_stage_tiles_allows_union_formula_without_intersection_mask(self):
        db = RecordingDb()

        _stage_tiles_and_pieces(
            db,
            source_region_code="TR-SOURCE",
            clip_region_code="TR-SOURCE",
            source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
            intersect_layers=[],
            difference_layers=["osm_nonforest_exclusion"],
            projected_srid=6933,
            tile_size_m=10_000,
            snap_grid_m=0.01,
            subdivide_max_vertices=256,
        )

        combined_sql = "\n".join(db.sql)
        self.assertIn("CREATE TEMP TABLE stage3_fragment_intersect_tiles", combined_sql)
        self.assertIn("FROM stage3_fragment_tiles", combined_sql)
        self.assertIn("ST_Difference(", combined_sql)

    def test_connected_component_graph_connects_subdivided_pieces_inside_same_tile(self):
        db = SameTileComponentDb()

        state = _build_connected_components(
            db,
            min_mask_component_area_ha=1.0,
            edge_tolerance_m=0.02,
        )

        self.assertEqual(state["connected_component_count"], 1)
        self.assertEqual(state["accepted_connected_component_count"], 1)
        self.assertEqual(state["accepted_piece_count"], 2)
        self.assertEqual(state["rejected_piece_count"], 0)
        combined_sql = "\n".join(db.sql)
        self.assertNotIn("left_piece.i <> right_piece.i", combined_sql)

    def test_full_turkey_v10_prepare_requires_explicit_user_approval(self):
        with self.assertRaisesRegex(RuntimeError, "Full Turkey V10 generation is locked"):
            require_user_approved_full_turkey(
                approved=False,
                region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                clip_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
            )

    def test_full_turkey_v10_grid_requires_explicit_user_approval(self):
        with self.assertRaisesRegex(RuntimeError, "Full Turkey V10 generation is locked"):
            require_user_approved_full_turkey(
                approved=False,
                grid_version=STAGE3_PUBLIC_V9_GRID_VERSION,
            )

    def test_national_scale_alias_scope_requires_explicit_user_approval(self):
        with self.assertRaisesRegex(RuntimeError, "Full Turkey V10 generation is locked"):
            require_user_approved_full_turkey(
                approved=False,
                region_code="TR-TURKEY-ALIAS",
                grid_version="alias-v9",
                scope_area_ha=1_000_001,
            )

    def test_v9_pilot_scope_does_not_require_full_turkey_approval(self):
        require_user_approved_full_turkey(
            approved=False,
            region_code=f"{STAGE3_PUBLIC_V9_REGION_CODE}-PILOT",
            grid_version=f"{STAGE3_PUBLIC_V9_GRID_VERSION}-pilot-final2",
            source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
            clip_region_code="TR-STAGE3-V9-PILOT-CLIP",
        )

    def test_full_turkey_v9_prepare_requires_exact_client_formula(self):
        with self.assertRaisesRegex(RuntimeError, "exact client formula"):
            require_stage3_public_v9_full_formula(
                region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                clip_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                source_layers=("ogm_esatis_bolme",),
                intersect_source_layers=("osm_forest_mask",),
                difference_source_layers=("osm_nonforest_exclusion",),
                min_mask_component_area_ha=1.0,
            )

    def test_national_scale_alias_prepare_cannot_bypass_exact_client_formula(self):
        with self.assertRaisesRegex(RuntimeError, "region_code"):
            require_stage3_public_v9_full_formula(
                region_code="TR-TURKEY-ALIAS",
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                clip_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                source_layers=("ogm_esatis_bolme", "osm_forest_mask"),
                intersect_source_layers=(),
                difference_source_layers=("osm_nonforest_exclusion",),
                min_mask_component_area_ha=1.0,
                scope_area_ha=1_000_001,
            )

    def test_full_turkey_v9_prepare_allows_only_exact_client_formula(self):
        require_stage3_public_v9_full_formula(
            region_code=STAGE3_PUBLIC_V9_REGION_CODE,
            source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
            clip_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
            source_layers=("ogm_esatis_bolme", "osm_forest_mask"),
            intersect_source_layers=(),
            difference_source_layers=("osm_nonforest_exclusion",),
            min_mask_component_area_ha=1.0,
        )

    def test_full_turkey_v9_grid_requires_exact_client_grid_args(self):
        with self.assertRaisesRegex(RuntimeError, "finalized client-formula mask"):
            require_stage3_public_v9_full_grid_args(
                region_code=STAGE3_PUBLIC_V9_REGION_CODE,
                grid_version=STAGE3_PUBLIC_V9_GRID_VERSION,
                grid_size_m=100,
                projected_srid=6933,
                tile_size_m=10000,
                use_region_mask_components=False,
                use_source_mask_cache=True,
                grid_from_source_mask=False,
                min_mask_component_area_ha=1.0,
            )

    def test_national_scale_alias_grid_cannot_bypass_exact_client_grid_args(self):
        with self.assertRaisesRegex(RuntimeError, "region_code"):
            require_stage3_public_v9_full_grid_args(
                region_code="TR-TURKEY-ALIAS",
                grid_version="alias-v9",
                grid_size_m=100,
                projected_srid=6933,
                tile_size_m=10000,
                use_region_mask_components=True,
                use_source_mask_cache=False,
                grid_from_source_mask=False,
                min_mask_component_area_ha=1.0,
                scope_area_ha=1_000_001,
            )

    def test_full_turkey_v9_grid_allows_only_exact_client_grid_args(self):
        require_stage3_public_v9_full_grid_args(
            region_code=STAGE3_PUBLIC_V9_REGION_CODE,
            grid_version=STAGE3_PUBLIC_V9_GRID_VERSION,
            grid_size_m=100,
            projected_srid=6933,
            tile_size_m=10000,
            use_region_mask_components=True,
            use_source_mask_cache=False,
            grid_from_source_mask=False,
            min_mask_component_area_ha=1.0,
        )

    def test_full_turkey_v9_source_cache_requires_exact_formula_cache_scope(self):
        with self.assertRaisesRegex(RuntimeError, "source cache"):
            require_stage3_public_v9_full_source_cache_args(
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                source_layers=("ogm_esatis_bolme", "osm_nonforest_exclusion"),
                projected_srid=6933,
            )

    def test_full_turkey_v9_source_cache_rejects_early_forest_component_filter(self):
        with self.assertRaisesRegex(RuntimeError, "min_forest_component_area_ha"):
            require_stage3_public_v9_full_source_cache_args(
                source_region_code=STAGE3_PUBLIC_SOURCE_REGION_CODE,
                source_layers=("ogm_esatis_bolme", "osm_forest_mask", "osm_nonforest_exclusion"),
                projected_srid=6933,
                min_forest_component_area_ha=1.0,
            )

    def test_national_scale_alias_source_cache_cannot_bypass_full_scope(self):
        with self.assertRaisesRegex(RuntimeError, "source_region_code"):
            require_stage3_public_v9_full_source_cache_args(
                source_region_code="TR-TURKEY-ALIAS",
                source_layers=("ogm_esatis_bolme", "osm_forest_mask", "osm_nonforest_exclusion"),
                projected_srid=6933,
                scope_area_ha=1_000_001,
            )


if __name__ == "__main__":
    unittest.main()
