import unittest
import inspect
import io
import sys
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.commands import prepare_stage3_source_mask_cache as source_mask_cache_command
from app.commands.prepare_stage3_source_mask_cache import (
    _cache_shard_exists,
    _delete_cache_shard,
    _insert_cache,
    _parse_source_layers,
    _validate_cache_source_layers,
    _validate_source_id_shard,
)


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class RecordingDb:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))
        return ScalarResult(3)


class CacheTransactionDb(RecordingDb):
    def __init__(self):
        super().__init__()
        self.committed = {"selected-shard": "old", "other-shard": "untouched"}
        self.pending = dict(self.committed)
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.pending = dict(self.committed)

    def commit(self):
        self.committed = dict(self.pending)
        self.commits += 1

    def execute(self, statement, params=None):
        result = super().execute(statement, params)
        if "DELETE FROM stage3_source_mask_cache" in str(statement):
            self.pending.pop("selected-shard", None)
        return result


class Stage3SourceMaskCacheTest(unittest.TestCase):
    def _replace_cli(self, db, *, shard, failure=None):
        layers = ["ogm_esatis_bolme", "osm_forest_mask", "osm_nonforest_exclusion"]
        counts = {layer: 1 for layer in layers}
        if failure == "missing-source":
            counts[layers[-1]] = 0

        def insert(*args, **kwargs):
            db.pending["selected-shard"] = "new"
            if failure == "insert":
                raise RuntimeError("fixture insert failure")
            return 3

        flags = ["--replace-shard", "--source-id-modulo", "2", "--source-id-remainder", "1"] if shard else ["--replace"]
        with ExitStack() as stack:
            for name, mock in {
                "SessionLocal": patch.object(source_mask_cache_command, "SessionLocal", return_value=db),
                "_region_area_ha": patch.object(source_mask_cache_command, "_region_area_ha", return_value=4),
                "_cache_counts": patch.object(source_mask_cache_command, "_cache_counts", return_value={k: 1 for k in layers}),
                "_source_counts": patch.object(source_mask_cache_command, "_source_counts", return_value=counts),
                "_insert_cache": patch.object(source_mask_cache_command, "_insert_cache", side_effect=insert),
                "_cache_quality": patch.object(source_mask_cache_command, "_cache_quality", return_value={}),
            }.items():
                stack.enter_context(mock)
            stack.enter_context(patch.object(sys, "argv", ["cache", "--source-region-code", "TR-SMALL",
                "--source-layers", ",".join(layers), "--projected-srid", "6933", *flags]))
            stack.enter_context(redirect_stdout(io.StringIO()))
            source_mask_cache_command.main()

    def test_missing_source_does_not_delete_existing_cache(self):
        for shard in (False, True):
            with self.subTest(shard=shard):
                db = CacheTransactionDb()
                with self.assertRaisesRegex(RuntimeError, "Missing source"):
                    self._replace_cli(db, shard=shard, failure="missing-source")
                self.assertEqual(db.commits, 0)
                self.assertEqual(db.committed["selected-shard"], "old")
                self.assertFalse(any("DELETE" in sql for sql, _ in db.statements))

    def test_failed_replacement_rolls_back_deletion_and_partial_insert(self):
        for shard in (False, True):
            with self.subTest(shard=shard):
                db = CacheTransactionDb()
                with self.assertRaisesRegex(RuntimeError, "fixture insert failure"):
                    self._replace_cli(db, shard=shard, failure="insert")
                self.assertEqual(db.commits, 0)
                self.assertEqual(db.pending, {"selected-shard": "old", "other-shard": "untouched"})

    def test_successful_replacement_commits_once(self):
        for shard in (False, True):
            with self.subTest(shard=shard):
                db = CacheTransactionDb()
                self._replace_cli(db, shard=shard)
                self.assertEqual(db.commits, 1)
                self.assertEqual(db.committed, {"selected-shard": "new", "other-shard": "untouched"})

    def test_runtime_role_script_preserves_stage3_mask_grants(self):
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "configure-db-runtime-role.sh"
        if not script_path.exists():
            self.skipTest("repository scripts are not mounted in this test image")
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("stage3_source_mask_cache", source)
        self.assertIn("stage3_region_mask_components", source)
        self.assertIn("GRANT DELETE ON TABLE public.%I TO %I", source)
        self.assertIn("GRANT USAGE, SELECT ON ALL SEQUENCES", source)

    def test_full_turkey_source_cache_entrypoint_has_approval_guard(self):
        source = inspect.getsource(source_mask_cache_command.main)

        self.assertIn("STAGE3_PUBLIC_V9_FULL_APPROVAL_FLAG", source)
        self.assertIn("require_stage3_public_v9_full_source_cache_args", source)
        self.assertIn("require_user_approved_full_turkey", source)
        self.assertIn("min_forest_component_area_ha=args.min_forest_component_area_ha", source)

    def test_parse_source_layers_trims_empty_values(self):
        self.assertEqual(
            _parse_source_layers("osm_forest_mask, , osm_nonforest_exclusion"),
            ["osm_forest_mask", "osm_nonforest_exclusion"],
        )

    def test_cache_source_layers_accept_public_forest_and_nonforest_sources(self):
        _validate_cache_source_layers(
            ["osm_forest_mask", "osm_nonforest_exclusion", "esa_worldcover_nonforest_exclusion"]
        )

    def test_cache_source_layers_reject_unknown_sources(self):
        with self.assertRaisesRegex(RuntimeError, "raw_unknown_cadastre"):
            _validate_cache_source_layers(["raw_unknown_cadastre"])

    def test_source_id_shard_requires_valid_remainder(self):
        _validate_source_id_shard(4, 3)
        with self.assertRaisesRegex(ValueError, "source_id_remainder"):
            _validate_source_id_shard(4, 4)
        with self.assertRaisesRegex(ValueError, "source_id_remainder"):
            _validate_source_id_shard(0, 1)

    def test_insert_cache_materializes_valid_subdivided_projected_components(self):
        db = RecordingDb()

        inserted = _insert_cache(
            db,
            source_region_code="TR-SOURCE",
            source_layers=["osm_forest_mask"],
            projected_srid=6933,
            max_vertices=256,
            source_id_modulo=4,
            source_id_remainder=2,
        )

        self.assertEqual(inserted, 3)
        insert_sql, params = db.statements[0]
        self.assertIn("ST_Subdivide(valid_components.geom_4326, :max_vertices)", insert_sql)
        self.assertIn("ST_Transform(geom_4326, :projected_srid)", insert_sql)
        self.assertIn("encode(ST_AsEWKB(geom_projected), 'hex')", insert_sql)
        self.assertIn("mod(parcel.id, :source_id_modulo) = :source_id_remainder", insert_sql)
        self.assertIn("ST_Intersection(ST_MakeValid(parcel.geom), source_region.geom) AS source_geom", insert_sql)
        self.assertIn("'clipped_to_source_region', true", insert_sql)
        self.assertIn("'pipeline', 'stage3_source_mask_cache'", insert_sql)
        self.assertEqual(params["source_region_code"], "TR-SOURCE")
        self.assertEqual(params["source_layers"], ["osm_forest_mask"])
        self.assertEqual(params["projected_srid"], 6933)
        self.assertEqual(params["source_id_modulo"], 4)
        self.assertEqual(params["source_id_remainder"], 2)

    def test_insert_cache_filters_tiny_forest_components_but_keeps_exclusions(self):
        db = RecordingDb()

        _insert_cache(
            db,
            source_region_code="TR-SOURCE",
            source_layers=["osm_forest_mask", "osm_nonforest_exclusion"],
            projected_srid=6933,
            max_vertices=256,
            min_forest_component_area_ha=1.0,
        )

        insert_sql, params = db.statements[0]
        self.assertIn("min_forest_component_area_ha", insert_sql)
        self.assertIn("component_area_filtered_layers", insert_sql)
        self.assertLess(insert_sql.index(":min_forest_component_area_ha"), insert_sql.index("ST_Subdivide"))
        self.assertNotIn("ST_Area(geom_projected) / 10000.0 >= :min_forest_component_area_ha", insert_sql)
        self.assertEqual(params["component_area_filtered_layers"], ["osm_forest_mask"])
        self.assertEqual(params["min_forest_component_area_ha"], 1.0)

    def test_cache_shard_helpers_scope_by_metadata(self):
        db = RecordingDb()

        _delete_cache_shard(
            db,
            "TR-SOURCE",
            ["osm_forest_mask"],
            6933,
            64,
            17,
        )
        exists = _cache_shard_exists(
            db,
            "TR-SOURCE",
            ["osm_forest_mask"],
            6933,
            64,
            17,
        )

        self.assertTrue(exists)
        delete_sql, delete_params = db.statements[0]
        exists_sql, exists_params = db.statements[1]
        self.assertIn("metadata->>'source_id_modulo' = :source_id_modulo", delete_sql)
        self.assertIn("metadata->>'source_id_remainder' = :source_id_remainder", delete_sql)
        self.assertIn("SELECT EXISTS", exists_sql)
        self.assertEqual(delete_params["source_id_modulo"], "64")
        self.assertEqual(delete_params["source_id_remainder"], "17")
        self.assertEqual(exists_params["source_id_modulo"], "64")
        self.assertEqual(exists_params["source_id_remainder"], "17")


if __name__ == "__main__":
    unittest.main()
