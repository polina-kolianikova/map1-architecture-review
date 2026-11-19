import inspect
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.commands.ingest_osm_pbf_masks import (
    DEFAULT_PBF_URL,
    FOREST_WHERE,
    NONFOREST_LINE_WHERE_FOR_METADATA,
    NONFOREST_LINE_WHERE,
    NONFOREST_POLYGON_TAG_FILTERS,
    NONFOREST_POLYGON_WHERE,
    NONFOREST_POLYGON_WHERE_FOR_METADATA,
    _polygon_exclusion_type_sql,
    _polygon_source_attributes_sql,
    activate_inserted_rows,
    activate_inserted_run,
    download_file,
    filter_expression_hash,
    filter_osm_pbf,
    filter_signature_matches,
    forest_id_batches,
    import_osm_layer,
    resolve_pbf_path,
    staging_table_name,
    store_nonforest_exclusion_component,
    store_nonforest_line_exclusions,
    store_nonforest_polygon_exclusions,
)


class OsmPbfMasksTest(unittest.TestCase):
    def test_import_rejects_partial_output_even_when_gdal_exits_zero(self):
        for returncode, diagnostics in (
            (0, "ERROR 1: Failed inserting way: database disk image is malformed\n"),
            (1, "Import interrupted"),
        ):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.pbf"
                source.write_bytes(b"fixture")
                completed = subprocess.CompletedProcess([], returncode, "", diagnostics)
                with patch("app.commands.ingest_osm_pbf_masks.shutil.disk_usage", return_value=SimpleNamespace(free=64 * 1024**3)), \
                     patch("app.commands.ingest_osm_pbf_masks.subprocess.run", return_value=completed), \
                     patch("app.commands.ingest_osm_pbf_masks.SessionLocal") as session:
                    with self.assertRaisesRegex(RuntimeError, "ogr2ogr import failed"):
                        import_osm_layer(pbf_path=source, table_name="test_import", osm_layer="multipolygons", where="1=1", ogr2ogr_path="ogr2ogr", temp_dir=root / "scratch")
                    session.assert_not_called()
                self.assertEqual(list((root / "scratch").iterdir()), [])

    def test_import_uses_separate_volume_scratch_and_cleans_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pbf"
            source.write_bytes(b"fixture")
            scratch_paths = []

            def run(_command, **kwargs):
                scratch = Path(kwargs["env"]["CPL_TMPDIR"])
                self.assertEqual(scratch.parent, root / "scratch")
                self.assertTrue(scratch.is_dir())
                self.assertEqual(kwargs["env"]["TMPDIR"], str(scratch))
                scratch_paths.append(scratch)
                (scratch / "temporary-index.db").write_bytes(b"index")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch.dict(os.environ, {"CPL_TMPDIR": "/unusable", "TMPDIR": "/unusable"}), \
                 patch("app.commands.ingest_osm_pbf_masks.shutil.disk_usage", return_value=SimpleNamespace(free=64 * 1024**3)), \
                 patch("app.commands.ingest_osm_pbf_masks.subprocess.run", side_effect=run), \
                 patch("app.commands.ingest_osm_pbf_masks.SessionLocal") as session:
                session.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 68
                for _ in range(2):
                    self.assertEqual(import_osm_layer(pbf_path=source, table_name="test_import", osm_layer="multipolygons", where="1=1", ogr2ogr_path="ogr2ogr", temp_dir=root / "scratch"), 68)
            self.assertNotEqual(*scratch_paths)
            self.assertEqual(list((root / "scratch").iterdir()), [])

    def test_import_refuses_insufficient_index_space(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pbf"
            source.write_bytes(b"fixture")
            with patch("app.commands.ingest_osm_pbf_masks.shutil.disk_usage", return_value=SimpleNamespace(free=1024)), \
                 patch("app.commands.ingest_osm_pbf_masks.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Insufficient scratch space"):
                    import_osm_layer(pbf_path=source, table_name="test_import", osm_layer="multipolygons", where="1=1", ogr2ogr_path="ogr2ogr", temp_dir=root / "scratch")
                run.assert_not_called()

    def test_staging_table_name_is_stable_and_safe(self):
        self.assertEqual(
            staging_table_name("stage3_osm_pbf_forest", "TR-STAGE3 PUBLIC"),
            "stage3_osm_pbf_forest_tr_stage3_public",
        )

    def test_long_region_names_fit_postgres_without_aliasing(self):
        prefix = "stage3_osm_pbf_nonforest_poly"
        regions = (
            "TR-HISTORICAL-ANCHOR-20261104-SOURCE",
            "TR-HISTORICAL-ANCHOR-20261104-SOURCE-SECOND",
            "TR-HISTORICAL-ANCHOR-20261104-SOURCE-THIRD",
        )
        names = [staging_table_name(prefix, region) for region in regions]
        self.assertEqual(len(set(names)), len(regions))
        for region, name in zip(regions, names):
            self.assertLessEqual(len(name.encode()), 63)
            self.assertRegex(name, r"^[a-z][a-z0-9_]*$")
            self.assertEqual(name, staging_table_name(prefix, region))

    def test_long_name_does_not_hide_invalid_prefix(self):
        with self.assertRaisesRegex(ValueError, "prefix is invalid"):
            staging_table_name("stage3_" + "x" * 80 + ";drop", "TR-SOURCE")

    def test_resolve_pbf_path_uses_geofabrik_filename(self):
        pbf = resolve_pbf_path(
            pbf_path="",
            pbf_url=DEFAULT_PBF_URL,
            fallback_pbf_urls=[],
            download_dir="/tmp/map1-osm",
            skip_download=True,
            dry_run=True,
        )

        self.assertEqual(pbf.path, Path("/tmp/map1-osm/turkey-latest.osm.pbf"))
        self.assertEqual(pbf.source_url, DEFAULT_PBF_URL)

    def test_download_file_can_force_refresh_existing_pbf(self):
        with tempfile.TemporaryDirectory() as directory:
            pbf_path = Path(directory) / "turkey-latest.osm.pbf"
            pbf_path.write_bytes(b"old")

            def fake_download_once(_url, path):
                path.write_bytes(b"fresh")

            with patch("app.commands.ingest_osm_pbf_masks._download_file_once", side_effect=fake_download_once) as download:
                source_url = download_file((DEFAULT_PBF_URL,), pbf_path, force=True)

        self.assertEqual(source_url, DEFAULT_PBF_URL)
        self.assertEqual(download.call_count, 1)

    def test_bulk_where_clauses_match_stage3_public_mask_scope(self):
        self.assertIn("natural = 'wood'", FOREST_WHERE)
        self.assertIn("landuse = 'forest'", FOREST_WHERE)
        self.assertEqual(NONFOREST_POLYGON_WHERE, "1=1")
        self.assertEqual(NONFOREST_LINE_WHERE, "1=1")
        self.assertIn("building IS NOT NULL", NONFOREST_POLYGON_WHERE_FOR_METADATA)
        self.assertIn("amenity IS NOT NULL", NONFOREST_POLYGON_WHERE_FOR_METADATA)
        self.assertIn("barrier IS NOT NULL", NONFOREST_POLYGON_WHERE_FOR_METADATA)
        self.assertIn("'farmland'", NONFOREST_POLYGON_WHERE_FOR_METADATA)
        self.assertIn("'bare_rock'", NONFOREST_POLYGON_WHERE_FOR_METADATA)
        self.assertIn("highway IS NOT NULL", NONFOREST_LINE_WHERE_FOR_METADATA)
        self.assertIn("railway IS NOT NULL", NONFOREST_LINE_WHERE_FOR_METADATA)
        self.assertIn("waterway IS NOT NULL", NONFOREST_LINE_WHERE_FOR_METADATA)
        self.assertIn("wr/building", NONFOREST_POLYGON_TAG_FILTERS)
        self.assertIn("wr/amenity", NONFOREST_POLYGON_TAG_FILTERS)
        self.assertIn("wr/barrier", NONFOREST_POLYGON_TAG_FILTERS)
        self.assertIn("wr/leisure=pitch", NONFOREST_POLYGON_TAG_FILTERS)
        self.assertNotIn("wr/leisure", NONFOREST_POLYGON_TAG_FILTERS)
        self.assertNotIn("wr/leisure=nature_reserve", NONFOREST_POLYGON_TAG_FILTERS)
        self.assertNotIn("leisure IS NOT NULL", NONFOREST_POLYGON_WHERE_FOR_METADATA)

    def test_filter_osm_pbf_refreshes_stale_filtered_file(self):
        with tempfile.TemporaryDirectory() as directory:
            pbf_path = Path(directory) / "turkey-latest.osm.pbf"
            output_path = Path(directory) / "turkey-latest.osm.forest.pbf"
            pbf_path.write_bytes(b"new")
            output_path.write_bytes(b"old")
            os.utime(output_path, (pbf_path.stat().st_mtime - 10, pbf_path.stat().st_mtime - 10))

            def fake_run(_command, text, capture_output, check):
                output_path.write_bytes(b"fresh")
                os.utime(output_path, None)

                class Completed:
                    returncode = 0
                    stderr = ""
                    stdout = ""

                return Completed()

            with patch("app.commands.ingest_osm_pbf_masks.shutil.which", return_value="/usr/bin/osmium"), patch(
                "app.commands.ingest_osm_pbf_masks.subprocess.run",
                side_effect=fake_run,
            ) as run:
                result = filter_osm_pbf(
                    pbf_path=pbf_path,
                    profile="forest",
                    expressions=("wr/natural=wood",),
                    osmium_path="osmium",
                )

            self.assertEqual(result, output_path)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(
                filter_signature_matches(
                    output_path.with_name(output_path.name + ".filter.json"),
                    profile="forest",
                    expressions=("wr/natural=wood",),
                )
            )

    def test_filter_osm_pbf_refreshes_when_filter_expression_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            pbf_path = Path(directory) / "turkey-latest.osm.pbf"
            output_path = Path(directory) / "turkey-latest.osm.nonforest_polygon.pbf"
            signature_path = output_path.with_name(output_path.name + ".filter.json")
            pbf_path.write_bytes(b"source")
            output_path.write_bytes(b"old-filter")
            signature_path.write_text(
                '{"profile":"nonforest_polygon","expressions_sha256":"'
                + filter_expression_hash(("wr/building",))
                + '"}\n',
                encoding="utf-8",
            )

            def fake_run(_command, text, capture_output, check):
                output_path.write_bytes(b"new-filter")

                class Completed:
                    returncode = 0
                    stderr = ""
                    stdout = ""

                return Completed()

            with patch("app.commands.ingest_osm_pbf_masks.shutil.which", return_value="/usr/bin/osmium"), patch(
                "app.commands.ingest_osm_pbf_masks.subprocess.run",
                side_effect=fake_run,
            ) as run:
                result = filter_osm_pbf(
                    pbf_path=pbf_path,
                    profile="nonforest_polygon",
                    expressions=("wr/building", "wr/amenity"),
                    osmium_path="osmium",
                )

            self.assertEqual(result, output_path)
            self.assertEqual(output_path.read_bytes(), b"new-filter")
            self.assertEqual(run.call_count, 1)

    def test_component_activation_keeps_other_nonforest_components_current(self):
        class FakeDb:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params):
                self.calls.append((str(statement), params))

        db = FakeDb()
        activated = activate_inserted_rows(
            db,
            source_layer="osm_nonforest_exclusion",
            region_code="TR-PUBLIC-FULL",
            inserted_ids=[1, 2],
            component="polygon",
        )

        self.assertEqual(activated, 2)
        self.assertIn("exclusion_component", db.calls[0][0])
        self.assertEqual(db.calls[0][1]["component"], "polygon")

    def test_forest_id_batches_uses_materialized_source_layer_batches(self):
        class Result:
            def mappings(self):
                return self

            def all(self):
                return [{"min_id": 10, "max_id": 20}, {"min_id": 50, "max_id": 61}]

        class FakeDb:
            def __init__(self):
                self.params = None

            def execute(self, *_args, **_kwargs):
                self.params = _args[1]
                return Result()

        db = FakeDb()

        self.assertEqual(
            list(
                forest_id_batches(
                    db,
                    region_code="TR-PUBLIC-FULL",
                    source_layers=["ogm_esatis_bolme", "osm_forest_mask"],
                    batch_size=10,
                )
            ),
            [(10, 20), (50, 61)],
        )
        self.assertEqual(db.params["source_layers"], ["ogm_esatis_bolme", "osm_forest_mask"])

    def test_activate_inserted_run_uses_started_at_component_and_deduplication(self):
        class CountResult:
            def scalar_one(self):
                return 7

        class FakeDb:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params):
                self.calls.append((str(statement), params))
                return CountResult()

        db = FakeDb()
        activated = activate_inserted_run(
            db,
            source_layer="osm_nonforest_exclusion",
            region_code="TR-PUBLIC-FULL",
            component="line",
            started_at="2026-07-27T20:00:00+00:00",
            inserted_count=12,
        )

        self.assertEqual(activated, 7)
        self.assertIn("row_number() OVER", db.calls[1][0])
        self.assertIn("duplicate_rank = 1", db.calls[1][0])

    def test_empty_inserted_run_still_retires_previous_component_rows(self):
        class CountResult:
            def scalar_one(self):
                return 0

        class FakeDb:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params):
                self.calls.append((str(statement), params))
                return CountResult()

        db = FakeDb()
        activated = activate_inserted_run(
            db,
            source_layer="osm_nonforest_exclusion",
            region_code="TR-PUBLIC-FULL",
            component="polygon",
            started_at="2026-07-27T20:00:00+00:00",
            inserted_count=0,
        )

        self.assertEqual(activated, 0)
        self.assertEqual(len(db.calls), 1)
        self.assertIn("SET is_current = false", db.calls[0][0])

    def test_nonforest_component_storage_is_limited_to_current_forest_mask(self):
        source = inspect.getsource(store_nonforest_exclusion_component)

        self.assertIn("filter_source_layers", source)
        self.assertIn("forest_limited_source_sql", source)
        self.assertIn('base_geom_expression = "ST_MakeValid(source.geom)"', source)

    def test_polygon_sql_quotes_reserved_osm_columns(self):
        self.assertIn('NULLIF("natural", \'\')', _polygon_exclusion_type_sql(""))
        self.assertIn('NULLIF(source."natural", \'\')', _polygon_exclusion_type_sql("source."))
        self.assertIn('"other_tags"::text', _polygon_source_attributes_sql(""))
        self.assertIn('source."other_tags"::text', _polygon_source_attributes_sql("source."))

    def test_polygon_source_uses_exists_lookup_without_duplicate_forest_rows(self):
        source = inspect.getsource(store_nonforest_polygon_exclusions)

        self.assertIn("WHERE EXISTS", source)
        self.assertIn("forest.source_layer = ANY(:filter_source_layers)", source)
        self.assertIn("source.geom && forest.geom", source)
        self.assertIn("ST_Intersects(ST_MakeValid(source.geom), forest.geom)", source)
        self.assertIn("NULL::bigint AS forest_id", source)
        self.assertNotIn("JOIN LATERAL", source)
        self.assertNotIn("ST_MakeValid(forest.geom)", source)

    def test_line_source_limits_using_buffered_geometry(self):
        source = inspect.getsource(store_nonforest_line_exclusions)

        self.assertIn("FROM {table} raw", source)
        self.assertIn("ST_Buffer(ST_Transform(ST_MakeValid(raw.geom), :projected_srid), :line_buffer_m)", source)
        self.assertIn("source.geom && forest.geom", source)
        self.assertIn("ST_Intersects(source.geom, forest.geom)", source)
        self.assertNotIn("ST_Intersects(ST_MakeValid(raw.geom), forest.geom)", source)


if __name__ == "__main__":
    unittest.main()
