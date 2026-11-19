"""Offline gate control-flow tests; spatial negative controls are supplied separately.

Run: PYTHONPATH=backend python3 -B -m unittest discover -s scripts/tests
     -p test_stage3_fragment_source_cache_gate.py
No database connection or network call is made.
"""
import argparse
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

from sqlalchemy.exc import OperationalError


SCRIPT = Path(__file__).resolve().parents[1] / "stage3_v9_fragment_equivalence_gate.py"
SPEC = importlib.util.spec_from_file_location("fragment_source_cache_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def comparison(*, kind="parcel", **changes):
    return {
        "kind": kind, "source_parcel_id": 1 if kind == "parcel" else None,
        "source_layer": "osm_forest_mask", "raw_present": True, "cache_present": True,
        "invalid_cache_rows": 0, "symdiff_m2": 0.00005, **changes,
    }


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self.value


class Savepoint:
    def __init__(self):
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.rollback()

    def rollback(self):
        self.rolled_back = True


class Db:
    def __init__(self, *, region=True, raw=12, cache=24, rows=None, error=None):
        self.results = iter([None, region, raw, cache,
                             rows if rows is not None else [comparison(), comparison(kind="layer")]])
        self.calls = []
        self.error = error
        self.savepoint = Savepoint()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def begin_nested(self):
        return self.savepoint

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        if len(self.calls) == 5 and self.error:
            raise self.error
        return Result(next(self.results))


class GeometryGateTest(unittest.TestCase):
    def check(self, db, **changes):
        return gate.check_source_cache_geometry(
            db, source_region_code="TR-SOURCE", source_layers=["osm_forest_mask"],
            projected_srid=6933, **changes,
        )

    def test_valid_whole_scope_and_settings_restored(self):
        db = Db()
        result = self.check(db)
        self.assertTrue(result["ready"])
        self.assertEqual(result["scope"], "entire_source_region")
        self.assertEqual(result["max_symdiff_m2"], 0.0001)
        self.assertEqual(result["raw_rows_probed"], 12)
        self.assertEqual(result["cache_rows_probed"], 24)
        self.assertTrue(db.savepoint.rolled_back)
        self.assertEqual(db.calls[0][1], {"timeout": "45000"})
        self.assertEqual(db.calls[-1][1]["source_region_code"], "TR-SOURCE")

    def test_nonfinite_negative_or_missing_measurement_fails(self):
        for value in (None, float("nan"), float("inf"), -float("inf"), -1, 0.00010001):
            with self.subTest(value=value):
                result = self.check(Db(rows=[comparison(symdiff_m2=value), comparison(kind="layer")]))
                self.assertFalse(result["ready"])
                self.assertEqual(result["failed_comparisons"], 1)
                json.dumps(result, allow_nan=False)

    def test_parcel_and_layer_checks_independently_fail(self):
        for kind in ("parcel", "layer"):
            with self.subTest(kind=kind):
                rows = [comparison(), comparison(kind="layer")]
                next(row for row in rows if row["kind"] == kind)["symdiff_m2"] = 2.0
                result = self.check(Db(rows=rows))
                self.assertFalse(result["ready"])
                self.assertEqual(result["failure_examples"][0]["kind"], kind)

    def test_missing_or_orphan_parcel_and_invalid_piece_fail_even_at_zero_area(self):
        for changes in ({"raw_present": False}, {"cache_present": False}, {"invalid_cache_rows": 1}):
            with self.subTest(changes=changes):
                result = self.check(Db(rows=[comparison(symdiff_m2=0, **changes), comparison(kind="layer")]))
                self.assertFalse(result["ready"])

    def test_empty_comparison_or_missing_requested_layer_is_not_success(self):
        for rows in ([], [comparison()]):
            with self.subTest(rows=rows):
                self.assertFalse(self.check(Db(rows=rows))["ready"])

    def test_scope_caps_fail_before_any_geometry_query(self):
        for changes in ({"raw": 13}, {"cache": 25}):
            with self.subTest(changes=changes):
                db = Db(**changes)
                result = self.check(db, max_parcels=12, max_cache_rows=24)
                self.assertFalse(result["ready"])
                self.assertEqual(result["failure"], "scope_limit_exceeded_not_sampled")
                self.assertEqual(len(db.calls), 4)
                self.assertEqual(db.calls[2][1]["parcel_probe_limit"], 13)
                self.assertEqual(db.calls[3][1]["cache_probe_limit"], 25)
                self.assertTrue(db.savepoint.rolled_back)

    def test_missing_region_or_empty_scope_fails_before_geometry(self):
        for changes in ({"region": False}, {"raw": 0}, {"cache": 0}):
            with self.subTest(changes=changes):
                db = Db(**changes)
                self.assertFalse(self.check(db)["ready"])
                self.assertEqual(len(db.calls), 4)

    def test_timeout_or_topology_error_is_failed_readiness(self):
        db = Db(error=OperationalError("SELECT", {}, Exception("fixture timeout")))
        result = self.check(db)
        self.assertFalse(result["ready"])
        self.assertEqual(result["failure"], "source_cache_query_failed")
        self.assertTrue(db.savepoint.rolled_back)

    def test_invalid_options_are_rejected_before_database_access(self):
        for value in ("nan", "inf", "-inf", -1, "invalid", None):
            with self.subTest(tolerance=value):
                db = Db()
                with self.assertRaises(argparse.ArgumentTypeError):
                    self.check(db, max_symdiff_m2=value)
                self.assertEqual(db.calls, [])
        for option in ("max_parcels", "max_cache_rows", "statement_timeout_ms"):
            for value in (0, -1, 1.5, "nan"):
                with self.subTest(option=option, value=value):
                    db = Db()
                    with self.assertRaises(argparse.ArgumentTypeError):
                        self.check(db, **{option: value})
                    self.assertEqual(db.calls, [])
        self.assertTrue(self.check(Db(rows=[comparison(symdiff_m2=0), comparison(kind="layer", symdiff_m2=0)]),
                                   max_symdiff_m2=0)["ready"])

    def test_sql_scope_contract_and_independent_transform(self):
        # Spatial behavior is exercised by the supplied TEMP-only PostGIS SQL.
        sql = str(gate.source_cache_geometry_sql())
        self.assertNotIn("clip_region_code", sql)
        self.assertIn("parcel.attributes->>'region_code' = :source_region_code", sql)
        self.assertIn("WHERE source_region_code = :source_region_code", sql)
        self.assertIn("FULL JOIN cached c USING (source_parcel_id, source_layer)", sql)
        self.assertIn("ST_Transform(geom, :projected_srid)", sql)
        for forbidden in ("ST_SnapToGrid", "ST_Subdivide", "stage3_fragment", "10000.0", "LIMIT"):
            self.assertNotIn(forbidden, sql)


class EntrypointTest(unittest.TestCase):
    def run_main(self, *, flags=(), geometry=None, old_ready=True):
        original = {"checks": {"old_formula_check": old_ready, "ready": old_ready}, "old_field": 42}
        output = io.StringIO()
        db = Db()
        with ExitStack() as stack:
            session = stack.enter_context(patch.object(gate, "SessionLocal", return_value=db))
            checker = stack.enter_context(patch.object(gate, "check_source_cache_geometry", return_value=geometry))
            required = stack.enter_context(patch.object(gate, "_require_source_cache"))
            stack.enter_context(patch.object(gate, "_stage_tiles_and_pieces"))
            stack.enter_context(patch.object(gate, "_build_connected_components", return_value={}))
            stack.enter_context(patch.object(gate, "_report", return_value=copy.deepcopy(original)))
            stack.enter_context(patch.object(sys, "argv", [str(SCRIPT), "--region-code", "TR-MASK",
                "--source-region-code", "TR-SOURCE", "--clip-region-code", "TR-SMALLER-CLIP",
                "--mask-version", "TR-MASK", *flags]))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(io.StringIO()))
            code = 0
            try:
                gate.main()
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(output.getvalue()) if output.getvalue() else None, checker, required, session, db, original

    def test_default_does_not_query_geometry_or_change_report(self):
        code, report, checker, _, _, db, original = self.run_main()
        self.assertEqual(code, 0)
        checker.assert_not_called()
        self.assertEqual(report, original)
        self.assertEqual(len(db.calls), 1)

    def test_opt_in_failure_emits_false_and_skips_formula_rebuild(self):
        code, report, _, required, _, _, _ = self.run_main(
            flags=["--check-source-cache-geometry"], geometry={"ready": False})
        self.assertEqual(code, 1)
        self.assertFalse(report["checks"]["ready"])
        self.assertFalse(report["formula_checked"])
        required.assert_not_called()

    def test_opt_in_success_does_not_override_old_formula_failure(self):
        for old_ready in (False, True):
            with self.subTest(old_ready=old_ready):
                code, report, checker, _, _, db, _ = self.run_main(
                    flags=["--check-source-cache-geometry", "--source-cache-max-parcels", "490"],
                    geometry={"ready": True}, old_ready=old_ready)
                self.assertEqual(code, 0 if old_ready else 1)
                self.assertEqual(report["checks"]["ready"], old_ready)
                self.assertTrue(report["checks"]["source_cache_geometry_matches"])
                self.assertNotIn("clip_region_code", checker.call_args.kwargs)
                self.assertEqual(checker.call_args.kwargs["source_region_code"], "TR-SOURCE")
                self.assertEqual(checker.call_args.kwargs["max_parcels"], 490)
                self.assertIn("REPEATABLE READ", db.calls[0][0])

    def test_bad_cli_tolerance_is_rejected_before_session_creation(self):
        for value in ("nan", "inf", "-1", "-inf"):
            with self.subTest(value=value):
                code, _, _, _, session, _, _ = self.run_main(
                    flags=["--check-source-cache-geometry", "--source-cache-max-symdiff-m2=" + value])
                self.assertEqual(code, 2)
                session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
