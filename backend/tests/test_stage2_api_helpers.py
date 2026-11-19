import inspect
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from app.api import stage2
from app.api.stage2 import (
    ASSET_PUBLIC_COLUMNS,
    MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM,
    _asset_filter_params,
    _asset_no_evidence_count_query,
    _asset_no_evidence_filters_match,
    _asset_no_evidence_query,
    _asset_query,
    _asset_stage4_public_filtered_query,
    _asset_stage4_public_filters_match,
    _asset_ids_stage4_public_filtered_query,
    _asset_search_params,
    _stage4_public_tile_query,
    _mask_tile_min_component_area_ha,
    _mask_tile_simplify_m,
)
from app.core.config import Settings
from app.services.dates import valid_iso_date
from app.services.stage3_partitioned_reads import (
    ACTIVE_GRID_CELLS_TABLE,
    PARTITIONED_GRID_CELLS_TABLE,
    PARTITIONED_SENTINEL_OBSERVATIONS_TABLE,
    PARTITIONED_VERIFICATION_RESULTS_TABLE,
    V10_GRID_VERSION,
    V10_REGION_CODE,
    grid_cells_table_sql,
    is_v10_public_scope,
    sentinel_observations_table_sql,
    verification_results_table_sql,
)


class Stage2ApiHelperTest(unittest.TestCase):
    def test_signal_status_normalizes_empty_raster_samples_without_hiding_failures(self):
        query = stage2._observation_status_sql().replace("observation.metadata->>'status'", ":status")
        with sqlite3.connect(":memory:") as db:
            for status, expected in (
                ("no_sample_pixels", "no_valid_pixels"),
                ("no_valid_pixels", "no_valid_pixels"),
                ("ok", "ok"), ("request_failed", "request_failed"),
                ("no_ndvi", "no_ndvi"), ("unknown", "unknown"), (None, None),
            ):
                with self.subTest(status=status):
                    self.assertEqual(db.execute("SELECT " + query, {"status": status}).fetchone()[0], expected)
        with self.assertRaises(ValueError):
            stage2._observation_status_sql("untrusted alias")

    def test_valid_date_accepts_iso_calendar_date(self):
        self.assertTrue(valid_iso_date("2026-07-07"))

    def test_valid_date_rejects_invalid_calendar_date(self):
        self.assertFalse(valid_iso_date("2026-99-99"))
        self.assertFalse(valid_iso_date("2026-7-7"))

    def test_asset_filters_accept_baseline_carbon_estimate(self):
        params = _asset_filter_params(
            status="all",
            carbon_status="baseline_estimate",
            signal_status="all",
            observed_from="",
            observed_to="",
            search="",
        )

        self.assertEqual(params["carbon_status"], "baseline_estimate")

    def test_asset_public_columns_exclude_internal_carbon_provenance(self):
        self.assertNotIn("carbon_config_hash", ASSET_PUBLIC_COLUMNS)
        self.assertNotIn("carbon_coefficient_source", ASSET_PUBLIC_COLUMNS)
        self.assertNotIn("coefficient_source", ASSET_PUBLIC_COLUMNS)

    def test_asset_public_columns_include_area_coefficient(self):
        self.assertIn("area_coefficient", ASSET_PUBLIC_COLUMNS)

    def test_stage2_vector_tiles_use_preclipped_cell_geometry(self):
        source = inspect.getsource(stage2)

        self.assertIn("ST_CollectionExtract(ST_MakeValid(cell.geom), 3)", source)
        self.assertNotIn("ST_Intersection(cell.geom, cell.region_geom)", source)
        self.assertNotIn("ST_Transform(cell.geom, 3857)", source)

    def test_stage2_scope_does_not_filter_assets_by_ndvi_evidence(self):
        source = inspect.getsource(stage2)

        self.assertNotIn("MIN_FOREST_EVIDENCE_NDVI", source)
        self.assertNotIn("forest_evidence_ndvi >= :min_forest_evidence_ndvi", source)

    def test_stage2_layer_a_tile_uses_source_region_scope(self):
        source = inspect.getsource(stage2.layer_a_tile)

        self.assertIn("region_code: str | None = Query(None)", source)
        self.assertIn("grid_version: str | None = Query(None)", source)
        self.assertIn("stage1_params(region_code=region_code, grid_version=grid_version, db=db)", source)
        self.assertIn(":layer_a_region_code", source)

    def test_stage2_mask_tile_prefers_component_mask_over_region_fallback(self):
        source = inspect.getsource(stage2.mask_tile)

        self.assertIn("@router.get(\"/tiles/mask/{z}/{x}/{y}.mvt\")", inspect.getsource(stage2))
        self.assertIn("stage3_region_mask_components", source)
        self.assertIn("ST_AsMVT(mvtgeom, 'mask'", source)
        self.assertIn("COALESCE(", source)
        self.assertIn("component.projected_srid = :mask_projected_srid", source)
        self.assertIn("mask_scope", source)
        self.assertIn("has_components", source)
        self.assertIn('if mask_scope["has_components"]', source)
        self.assertIn("ST_SimplifyPreserveTopology", source)
        self.assertIn("ST_Transform({geom_column}, 3857)", source)
        self.assertIn("region.metadata->'forest_mask'->>'mask_version'", source)
        self.assertNotIn("MIN_RAW_TILE_ZOOM", source)
        self.assertNotIn("WHERE NOT EXISTS", source)

    def test_mask_tile_simplifies_low_zoom_only(self):
        self.assertGreater(_mask_tile_simplify_m(5), _mask_tile_simplify_m(9))
        self.assertEqual(_mask_tile_simplify_m(12), 0.0)

    def test_mask_tile_thins_components_at_overview_zoom(self):
        source = inspect.getsource(stage2.mask_tile)

        self.assertIn("component.area_ha >= :mask_min_component_area_ha", source)
        self.assertGreater(_mask_tile_min_component_area_ha(3), _mask_tile_min_component_area_ha(5))
        self.assertGreater(_mask_tile_min_component_area_ha(5), _mask_tile_min_component_area_ha(9))
        self.assertEqual(_mask_tile_min_component_area_ha(10), 0.0)

    def test_stage2_scope_can_be_overridden_for_assets_and_tiles(self):
        source = inspect.getsource(stage2)

        self.assertIn("region_code: str | None = Query(None)", source)
        self.assertIn("grid_version: str | None = Query(None)", source)
        self.assertIn("stage1_params(region_code=region_code, grid_version=grid_version)", source)
        self.assertIn("region_code=region_code", inspect.getsource(stage2._tile_params))

    def test_asset_pick_endpoint_uses_scoped_grid_intersection(self):
        source = inspect.getsource(stage2.pick_asset)
        module_source = inspect.getsource(stage2)

        self.assertLess(
            module_source.index('@router.get("/assets/pick")'),
            module_source.index('@router.get("/assets/{cell_uuid}")'),
        )
        self.assertIn("lng: float = Query(..., ge=-180, le=180)", source)
        self.assertIn("lat: float = Query(..., ge=-90, le=90)", source)
        self.assertIn("grid_cells_table_sql(params)", source)
        self.assertIn("ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)", source)
        self.assertIn("ST_DWithin(cell.geom, point.geom, :pick_tolerance_deg)", source)
        self.assertIn("ST_Intersects(cell.geom, point.geom)", source)
        self.assertIn("cell.grid_version = :grid_version", source)
        self.assertIn("Asset not found at map location", source)

    def test_asset_register_default_page_uses_stable_id_order(self):
        query = str(_asset_query("grid_cells"))

        self.assertIn("ORDER BY id", query)
        self.assertIn("region_scope AS", query)
        self.assertNotIn("latest_event_at DESC NULLS LAST", query)

    def test_unfiltered_asset_fast_path_preserves_real_evidence(self):
        source = inspect.getsource(stage2.list_assets)

        self.assertNotIn("_scope_has_verification_evidence", source)
        self.assertIn("partitioned_v10_reads", source)
        self.assertIn("v10_public_scope", source)
        self.assertIn("if not v10_public_scope", source)
        self.assertIn("_asset_stage4_public_unfiltered_query(grid_table)", source)
        self.assertIn("_asset_query(\"grid_cells\")", source)
        sql = str(stage2._asset_stage4_public_unfiltered_query(PARTITIONED_GRID_CELLS_TABLE))
        self.assertIn("verification.id AS verification_result_id", sql)
        self.assertLess(sql.index("LIMIT :query_limit"), sql.index("FROM verification_results"))

    def test_partitioned_asset_register_is_guarded_to_v10_flag_path(self):
        source = inspect.getsource(stage2.list_assets)
        ids_source = inspect.getsource(stage2.list_asset_ids)

        self.assertIn("use_partitioned_v10_grid_reads(params)", source)
        self.assertIn("use_partitioned_v10_grid_reads(params)", ids_source)
        self.assertIn("_asset_stage4_public_filtered_query(grid_table)", source)
        self.assertIn("_asset_ids_stage4_public_filtered_query(grid_table)", ids_source)
        self.assertIn("_asset_count_query(\"grid_cells\")", source)
        self.assertIn("_asset_count_query(\"grid_cells\")", ids_source)

    def test_filtered_asset_register_uses_stage4_public_fast_path(self):
        source = inspect.getsource(stage2.list_assets)

        self.assertIn("_asset_stage4_public_filtered_query(grid_table)", source)
        self.assertNotIn("has_verification", source)
        self.assertNotIn("_asset_stage4_public_summary_total", source)
        self.assertIn("_paged_total(rows, limit=limit, offset=offset)", source)

    def test_stage4_public_asset_reads_page_cells_before_observations(self):
        page_query = str(stage2._asset_stage4_public_unfiltered_query(PARTITIONED_GRID_CELLS_TABLE))
        detail_query = str(stage2._asset_stage4_public_detail_query(PARTITIONED_GRID_CELLS_TABLE))
        history_query = str(
            stage2._asset_stage4_public_history_query(
                PARTITIONED_GRID_CELLS_TABLE,
                PARTITIONED_SENTINEL_OBSERVATIONS_TABLE,
            )
        )
        history_source = inspect.getsource(stage2.get_asset_ndvi_history)

        self.assertIn("page_cells AS", page_query)
        self.assertIn("LIMIT :query_limit", page_query)
        self.assertIn("LEFT JOIN LATERAL", page_query)
        self.assertIn("observation.metadata->>'pipeline' = 'stage4_layer_b_ndvi'", page_query)
        self.assertIn("cell.uuid = CAST(:cell_uuid AS uuid)", detail_query)
        self.assertIn("target_cell AS", history_query)
        self.assertIn("anchor.metadata->>'pipeline' = 'stage4_layer_b_ndvi'", history_query)
        self.assertIn("SELECT DISTINCT ON (observation.acquisition_date)", history_query)
        self.assertIn("CASE WHEN observation.is_current THEN 0 ELSE 1 END", history_query)
        self.assertIn("is_v10_public_scope(params)", history_source)
        self.assertIn("_asset_stage4_public_history_query", history_source)

    def test_stage4_public_filtered_reads_keep_partitioned_fast_path(self):
        page_query = str(_asset_stage4_public_filtered_query(PARTITIONED_GRID_CELLS_TABLE))
        ids_query = str(_asset_ids_stage4_public_filtered_query(PARTITIONED_GRID_CELLS_TABLE))
        params = {
            **_asset_filter_params(
                status="unverified",
                carbon_status="awaiting_coefficients",
                signal_status="ok",
                observed_from="",
                observed_to="",
                search="da7547de",
            ),
            "carbon_calculation_status": "pending_coefficients",
        }

        self.assertTrue(_asset_stage4_public_filters_match(params))
        self.assertIn("FROM grid_cells_partitioned cell", page_query)
        self.assertIn("FROM grid_cells_partitioned cell", ids_query)
        self.assertIn("stage4_layer_b_ndvi", page_query)
        self.assertIn("COALESCE(observation.observation_status, 'unknown')", page_query)
        self.assertIn("_asset_query(\"grid_cells\")", inspect.getsource(stage2.list_assets))

    def test_no_evidence_asset_filters_match_synthetic_v10_state(self):
        params = {
            **_asset_filter_params(
                status="unverified",
                carbon_status="awaiting_coefficients",
                signal_status="unknown",
                observed_from="",
                observed_to="",
                search="da7547de",
            ),
            "carbon_calculation_status": "pending_coefficients",
        }

        self.assertTrue(_asset_no_evidence_filters_match(params))

        params["status"] = "ok"
        self.assertFalse(_asset_no_evidence_filters_match(params))

    def test_no_evidence_asset_search_uses_indexable_uuid_prefix(self):
        params = _asset_search_params("da7547de")

        self.assertTrue(params["has_uuid_prefix"])
        self.assertEqual(params["uuid_prefix_start"], "da7547de-0000-0000-0000-000000000000")
        self.assertEqual(params["uuid_prefix_end"], "da7547de-ffff-ffff-ffff-ffffffffffff")
        self.assertIsNone(params["search_uuid_exact"])

    def test_no_evidence_asset_queries_avoid_full_asset_cte(self):
        page_query = str(_asset_no_evidence_query("grid_cells"))
        count_query = str(_asset_no_evidence_count_query("grid_cells"))

        self.assertNotIn("latest_observation AS", page_query)
        self.assertNotIn("latest_result AS", page_query)
        self.assertIn("cell.uuid >= CAST(:uuid_prefix_start AS uuid)", page_query)
        self.assertIn("stage3_grid_generation_runs", count_query)
        self.assertIn("cell.uuid >= CAST(:uuid_prefix_start AS uuid)", count_query)

    def test_v10_partitioned_reads_are_disabled_by_default(self):
        params = {"region_code": V10_REGION_CODE, "grid_version": V10_GRID_VERSION}

        table = grid_cells_table_sql(params, settings=Settings())

        self.assertEqual(table, ACTIVE_GRID_CELLS_TABLE)

    def test_v10_partitioned_reads_only_match_exact_client_scope(self):
        enabled = Settings(stage3_v10_partitioned_grid_reads=True)

        self.assertEqual(
            grid_cells_table_sql(
                {"region_code": V10_REGION_CODE, "grid_version": V10_GRID_VERSION},
                settings=enabled,
            ),
            PARTITIONED_GRID_CELLS_TABLE,
        )
        self.assertEqual(
            grid_cells_table_sql(
                {"region_code": V10_REGION_CODE, "grid_version": "stage3-public-v9"},
                settings=enabled,
            ),
            ACTIVE_GRID_CELLS_TABLE,
        )
        self.assertEqual(
            grid_cells_table_sql(
                {"region_code": "TR-STAGE3-PILOT", "grid_version": V10_GRID_VERSION},
                settings=enabled,
            ),
            ACTIVE_GRID_CELLS_TABLE,
        )

    def test_v10_public_scope_matches_exact_client_scope(self):
        self.assertTrue(is_v10_public_scope({"region_code": V10_REGION_CODE, "grid_version": V10_GRID_VERSION}))
        self.assertFalse(is_v10_public_scope({"region_code": V10_REGION_CODE, "grid_version": "stage3-public-v9"}))

    def test_stage2_queries_can_use_partitioned_parent_without_direct_child_table(self):
        page_query = str(_asset_no_evidence_query(PARTITIONED_GRID_CELLS_TABLE))
        count_query = str(_asset_no_evidence_count_query(PARTITIONED_GRID_CELLS_TABLE))

        self.assertIn("FROM grid_cells_partitioned cell", page_query)
        self.assertIn("FROM grid_cells_partitioned cell", count_query)
        self.assertNotIn("grid_cells_partitioned_v10", page_query)
        self.assertNotIn("grid_cells_partitioned_v10", count_query)

    def test_evidence_reads_can_route_to_partitioned_tables(self):
        enabled = Settings(stage3_partitioned_evidence_reads=True)

        self.assertEqual(sentinel_observations_table_sql(settings=enabled), PARTITIONED_SENTINEL_OBSERVATIONS_TABLE)
        self.assertEqual(verification_results_table_sql(settings=enabled), PARTITIONED_VERIFICATION_RESULTS_TABLE)

        for function in (stage2.get_asset, stage2.get_asset_ndvi_history, stage2.layer_b_tile, stage2.layer_c_tile):
            source = inspect.getsource(function)

            self.assertTrue(
                "sentinel_observations_table_sql()" in source
                or "verification_results_table_sql()" in source
                or "_asset_scope_sql" in source
            )

    def test_layer_b_reads_prefer_stage4_ndvi_pipeline(self):
        source = inspect.getsource(stage2)

        self.assertIn("'stage4_layer_b_ndvi', 'stage1_cdse_ndvi'", source)
        self.assertIn("NDVI_PIPELINE_PRIORITY_SQL", source)
        self.assertIn("observation.metadata->>'pipeline' IN {NDVI_PIPELINE_SQL}", source)

    def test_v10_public_tiles_use_stage4_public_scope(self):
        sql = str(
            _stage4_public_tile_query(
                PARTITIONED_GRID_CELLS_TABLE,
                PARTITIONED_SENTINEL_OBSERVATIONS_TABLE,
                layer_name="layer_b",
            )
        )

        self.assertIn("cell.region_id = (SELECT id FROM region_scope)", sql)
        self.assertIn("observation.metadata->>'pipeline' = 'stage4_layer_b_ndvi'", sql)
        self.assertIn("ST_CollectionExtract(ST_MakeValid(joined.geom), 3)", sql)
        self.assertIn(stage2._asset_verification_status_sql(), sql)
        self.assertIn("stage1_verification", sql)
        self.assertIn("result.grid_cell_id = cell.id", sql)

    def test_v10_public_raw_tiles_start_above_overview_zoom(self):
        self.assertEqual(MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM, 13)

    def test_v10_public_layer_c_tile_only_shortcuts_below_raw_zoom(self):
        source = inspect.getsource(stage2.layer_c_tile)

        self.assertIn("if is_v10_public_scope(params) and z < MIN_STAGE4_PUBLIC_RAW_TILE_ZOOM:", source)
        self.assertIn("return _mvt_response(None)", source)
        self.assertIn("verification_results_table_sql()", source)
        self.assertIn("result.is_current", source)

    def test_non_v10_asset_register_flag_off_keeps_legacy_query_branch(self):
        db = _DummyDb()
        with (
            patch.object(stage2, "SessionLocal", return_value=_DummySession(db)),
            patch.object(stage2, "stage1_params", return_value={"region_code": "TR-OTHER", "grid_version": "grid-v1"}),
            patch.object(stage2, "use_partitioned_v10_grid_reads", return_value=False),
            patch.object(stage2, "is_v10_public_scope", return_value=False),
            patch.object(stage2, "_scope_has_verification_evidence", side_effect=AssertionError("unexpected evidence probe")),
            patch.object(stage2, "_asset_query", return_value="asset-query") as asset_query,
            patch.object(stage2, "_asset_count_query", return_value="asset-count") as asset_count,
            patch.object(stage2, "_asset_no_evidence_query", return_value="no-evidence-query") as no_evidence,
        ):
            result = stage2.list_assets(
                limit=100,
                offset=0,
                status="all",
                carbon_status="all",
                signal_status="all",
                observed_from="",
                observed_to="",
                search="",
            )

        self.assertEqual(result["rows"], [])
        asset_query.assert_called_once_with("grid_cells")
        asset_count.assert_not_called()
        no_evidence.assert_not_called()

    def test_asset_register_partitioned_flag_uses_v10_stage4_public_filtered_branch(self):
        db = _DummyDb()
        with (
            patch.object(stage2, "SessionLocal", return_value=_DummySession(db)),
            patch.object(
                stage2,
                "stage1_params",
                return_value={"region_code": V10_REGION_CODE, "grid_version": V10_GRID_VERSION},
            ),
            patch.object(stage2, "use_partitioned_v10_grid_reads", return_value=True),
            patch.object(stage2, "is_v10_public_scope", return_value=True),
            patch.object(stage2, "grid_cells_table_sql", return_value=PARTITIONED_GRID_CELLS_TABLE),
            patch.object(stage2, "_scope_has_verification_evidence", return_value=False),
            patch.object(stage2, "_asset_stage4_public_filtered_query", return_value="stage4-public-query") as public_query,
            patch.object(stage2, "_asset_query", return_value="asset-query") as asset_query,
        ):
            result = stage2.list_assets(
                limit=100,
                offset=0,
                status="unverified",
                carbon_status="all",
                signal_status="all",
                observed_from="",
                observed_to="",
                search="",
            )

        self.assertEqual(result["rows"], [])
        public_query.assert_called_once_with(PARTITIONED_GRID_CELLS_TABLE)
        asset_query.assert_not_called()

    def test_v10_asset_register_flag_off_uses_active_stage4_public_filtered_path(self):
        db = _DummyDb()
        with (
            patch.object(stage2, "SessionLocal", return_value=_DummySession(db)),
            patch.object(
                stage2,
                "stage1_params",
                return_value={"region_code": V10_REGION_CODE, "grid_version": V10_GRID_VERSION},
            ),
            patch.object(stage2, "use_partitioned_v10_grid_reads", return_value=False),
            patch.object(stage2, "is_v10_public_scope", return_value=True),
            patch.object(stage2, "_scope_has_verification_evidence", return_value=False),
            patch.object(stage2, "_asset_stage4_public_filtered_query", return_value="stage4-public-query") as public_query,
            patch.object(stage2, "_asset_query", return_value="asset-query") as asset_query,
        ):
            result = stage2.list_assets(
                limit=100,
                offset=0,
                status="unverified",
                carbon_status="all",
                signal_status="all",
                observed_from="",
                observed_to="",
                search="",
            )

        self.assertEqual(result["rows"], [])
        public_query.assert_called_once_with("grid_cells")
        asset_query.assert_not_called()


class V10EvidenceRegressionTest(unittest.TestCase):
    def test_generic_and_public_paths_share_scope_status_and_layer_c_search_params(self):
        self.assertIn(
            stage2._asset_verification_status_sql("latest_result"),
            stage2._asset_scope_sql("grid_cells"),
        )
        for function in (stage2.grid_tile, stage2.layer_b_tile, stage2.layer_c_tile):
            self.assertIn('_asset_verification_status_sql("latest_result")', inspect.getsource(function))
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = None
        with patch.object(stage2, "SessionLocal", return_value=_DummySession(db)):
            stage2.layer_c_tile(
                z=15, x=19457, y=12211, status="all", carbon_status="all",
                signal_status="all", observed_from="", observed_to="", search="12345",
                region_code="test-non-v10", grid_version="copied-two-cells",
            )
        query, params = db.execute.call_args.args
        self.assertIn("id = CAST(:search_cell_id AS bigint)", str(query))
        self.assertIn("replace(uuid, '-', '')", str(query))
        self.assertEqual(params["search_cell_id"], 12345)
        compact = stage2._asset_filter_params(
            status="all", carbon_status="all", signal_status="all",
            observed_from="", observed_to="", search="123456781234123412341234567890ab",
        )
        self.assertEqual(compact["search_uuid_exact"], "12345678-1234-1234-1234-1234567890ab")

    def test_partial_layer_c_scope_never_substitutes_carbon_for_missing_results(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE regions(id, code);
            INSERT INTO regions VALUES (5, 'V10'), (6, 'other');
            CREATE TABLE verification_results_partitioned(
                grid_cell_id,region_id,region_code,grid_version,is_current,anomaly_type,metadata);
            INSERT INTO verification_results_partitioned VALUES
                (1,5,'V10','g',1,'watch','{"pipeline":"stage1_verification"}'),
                (2,6,'other','g',1,'watch','{"pipeline":"stage1_verification"}');
            CREATE TABLE cells(id);
            INSERT INTO cells VALUES (1), (2);
            CREATE TABLE carbon_assessments(grid_cell_id, metadata);
            INSERT INTO carbon_assessments VALUES
                (1,'{"anomaly_type":"ok"}'), (2,'{"anomaly_type":"anomaly"}');
        """)
        with patch.object(stage2, "verification_results_table_sql", return_value=PARTITIONED_VERIFICATION_RESULTS_TABLE):
            status_sql = stage2._asset_verification_status_sql()
        query = f"""SELECT {status_sql} FROM cells cell
            LEFT JOIN verification_results_partitioned verification
              ON verification.grid_cell_id=cell.id AND verification.region_id=5
             AND verification.is_current
            LEFT JOIN carbon_assessments latest_carbon ON latest_carbon.grid_cell_id=cell.id
            ORDER BY cell.id"""
        params = {"region_code": "V10", "grid_version": "g"}
        self.assertEqual(db.execute(query, params).fetchall(), [("watch",), ("unverified",)])
        db.execute("UPDATE verification_results_partitioned SET is_current=0 WHERE region_id=5")
        # Current evidence in another region must not disable this scope's legacy fallback.
        self.assertEqual(db.execute(query, params).fetchall(), [("ok",), ("anomaly",)])

    def test_scoped_current_verification_and_audit_queries_execute(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE verification_results(id, grid_cell_id, region_id, region_code,
                grid_version, is_current, anomaly_type, score, explanation, metadata, created_at);
            CREATE TABLE verification_audit_events(grid_cell_id,region_id,region_code,
                grid_version,metadata,created_at);
            INSERT INTO verification_results VALUES
                (10,1,5,'V10','g',1,'ok',0,'actual old','{"pipeline":"stage1_verification"}','2026-10-01'),
                (11,1,5,'V10','g',1,'watch',0.2,'actual current','{"pipeline":"stage1_verification"}','2026-11-01'),
                (12,1,5,'V10','g',0,'anomaly',0.9,'retired','{"pipeline":"stage1_verification"}','2026-11-02'),
                (13,1,6,'other','g',1,'anomaly',0.9,'other scope','{"pipeline":"stage1_verification"}','2026-11-03');
            INSERT INTO verification_audit_events VALUES
                (1,5,'V10','g','{"pipeline":"stage1_verification_audit"}','2026-11-01'),
                (1,5,'V10','g','{"pipeline":"stage1_verification_audit"}','2026-11-02'),
                (1,6,'other','g','{"pipeline":"stage1_verification_audit"}','2026-11-03');
        """)
        joins = stage2._asset_stage4_public_verification_joins()
        select = joins.split("LEFT JOIN LATERAL (", 1)[1].split(") verification ON true", 1)[0]
        audit = joins.split("LEFT JOIN LATERAL (")[2].split(") verification_audit ON true", 1)[0]
        def execute(sql):
            sql = sql.replace("cell.id", ":cell_id").replace("(SELECT id FROM region_scope)", ":region_id")
            sql = sql.replace("::bigint", "").replace("::text", "")
            return db.execute(sql, {"cell_id":1,"region_id":5,"region_code":"V10","grid_version":"g"}).fetchone()
        row = execute(select)
        self.assertEqual(row[:4], (11, "watch", 0.2, "actual current"))
        self.assertEqual(execute(audit), (2, "2026-11-02"))

    def test_optimized_projection_contains_real_nullable_verification_fields(self):
        sql = stage2._asset_stage4_public_columns_sql("NULL::json")
        self.assertIn("verification.id AS verification_result_id", sql)
        self.assertIn("verification.score", sql)
        self.assertIn("verification_audit.audit_event_count", sql)
        self.assertIn("verification.metadata->>'layer_a_source_quality'", sql)
        self.assertNotIn("NULL::bigint AS verification_result_id", sql)
        self.assertNotIn("0::bigint AS audit_event_count", sql)
        with patch.object(stage2, "verification_results_table_sql", return_value=PARTITIONED_VERIFICATION_RESULTS_TABLE):
            self.assertIn(PARTITIONED_VERIFICATION_RESULTS_TABLE, str(stage2._asset_stage4_public_detail_query(PARTITIONED_GRID_CELLS_TABLE)))

    def test_filtered_reads_keep_partition_store_and_ignore_stale_summary(self):
        rows = [{"cell_uuid":str(i)} for i in range(3)]
        db = MagicMock()
        db.execute.return_value.mappings.return_value.__iter__.side_effect = lambda: iter(rows)
        params = {"region_code":V10_REGION_CODE,"grid_version":V10_GRID_VERSION}
        with (
            patch.object(stage2,"SessionLocal",return_value=_DummySession(db)),
            patch.object(stage2,"stage1_params",return_value=params),
            patch.object(stage2,"use_partitioned_v10_grid_reads",return_value=True),
            patch.object(stage2,"grid_cells_table_sql",return_value=PARTITIONED_GRID_CELLS_TABLE),
            patch.object(stage2,"_scope_has_verification_evidence",side_effect=AssertionError("Must not change stores when evidence appears")),
            patch.object(stage2,"_asset_stage4_public_summary_total",side_effect=AssertionError("Stale cache must not supply exact count")),
        ):
            response = stage2.list_assets(limit=2,offset=0,status="ok",carbon_status="all",signal_status="all",observed_from="",observed_to="",search="")
            ids = stage2.list_asset_ids(limit=2,status="ok",carbon_status="all",signal_status="all",observed_from="",observed_to="",search="")
        self.assertEqual(len(response["rows"]), 2)
        self.assertEqual(response["total"], 3)
        self.assertTrue(response["has_more"])
        self.assertFalse(response["exact_total"])
        self.assertEqual(ids["ids"], ["0", "1"])
        self.assertEqual(ids["total"], 3)
        self.assertTrue(ids["truncated"])
        self.assertFalse(ids["exact_total"])
        sql = str(db.execute.call_args.args[0])
        self.assertIn("FROM grid_cells_partitioned cell", sql)
        self.assertIn("LIMIT :query_limit", sql)

    def test_bounded_pagination_marks_only_exhausted_results_exact(self):
        self.assertEqual(stage2._paged_total([1,2,3],limit=2,offset=0),(3,False,True))
        self.assertEqual(stage2._paged_total([1],limit=2,offset=2),(3,True,False))
        self.assertEqual(stage2._paged_total([],limit=2,offset=20),(0,False,False))


class _DummyResult:
    def mappings(self):
        return self

    def __iter__(self):
        return iter([])

    def all(self):
        return []

    def one_or_none(self):
        return None

    def scalar_one(self):
        return 0


class _DummyDb:
    def execute(self, *_args, **_kwargs):
        return _DummyResult()


class _DummySession:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_args):
        return False


if __name__ == "__main__":
    unittest.main()
