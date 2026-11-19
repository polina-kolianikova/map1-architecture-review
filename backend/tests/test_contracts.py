import asyncio
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.main import app
from sqlalchemy.exc import OperationalError

UUID_VALUE = "00000000-0000-4000-8000-000000000001"


async def request(method, path, query=None, body=None):
    sent = []
    consumed = False
    raw = json.dumps(body).encode() if body is not None else b""
    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": urlencode(query or {}).encode(), "root_path": "", "headers": [(b"host", b"localhost"), (b"content-type", b"application/json")], "server": ("localhost", 8000), "client": ("localhost", 1)}
    await app(scope, receive, send)
    status = next(x["status"] for x in sent if x["type"] == "http.response.start")
    content = b"".join(x.get("body", b"") for x in sent if x["type"] == "http.response.body")
    return status, content


class CoreContractTests(unittest.TestCase):
    def call(self, *args, **kwargs):
        return asyncio.run(request(*args, **kwargs))

    def test_health_and_real_metadata(self):
        self.assertEqual(self.call("GET", "/health"), (200, b'{"status":"ok"}'))
        for path, field in [("/meta/localization", "supported_locales"), ("/stage1/source-strategy", "sources")]:
            status, body = self.call("GET", path)
            self.assertEqual(status, 200, body)
            self.assertIsInstance(json.loads(body), dict)

    def test_private_rules_only_are_501(self):
        for method, path, body in [("GET", "/stage5/rules", None), ("POST", "/stage5/rules/validate", {}), ("PUT", "/stage5/rules", {}), ("POST", "/stage5/rules/reset", None), ("GET", "/stage5/rules/history", None)]:
            with self.subTest(path=path, method=method):
                status, payload = self.call(method, path, body=body)
                self.assertEqual(status, 501, payload)
                self.assertIn("not distributed", json.loads(payload)["detail"])

    def test_missing_database_is_503_not_empty_success(self):
        for module, path in [("stage1", "/stage1/regions"), ("stage2", "/stage2/assets"), ("stage4", "/stage4/runs"), ("stage5", "/stage5/status")]:
            with self.subTest(path=path), patch("app.api." + module + ".SessionLocal", side_effect=OperationalError("read", {}, Exception("unavailable"))):
                status, payload = self.call("GET", path)
                self.assertEqual(status, 503, payload)
                self.assertNotIn("rows", json.loads(payload))

    def test_original_region_reader_returns_database_rows(self):
        captured = []
        row = {"id": 1, "name": "Review region", "code": "REVIEW-SCOPE", "country_code": "TR", "metadata": None, "geometry": None}
        class Result:
            def mappings(self): return iter([row])
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query, params):
                captured.append((str(query), params));return Result()
        with patch("app.api.stage1.SessionLocal", return_value=Database()), patch("app.api.stage1.stage1_params", return_value={"region_code": "REVIEW-SCOPE", "grid_version": "review-grid"}):
            status, payload = self.call("GET", "/stage1/regions", {"limit": 1})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)[0]["code"], "REVIEW-SCOPE")
        self.assertIn("FROM regions", captured[0][0])
        self.assertEqual(captured[0][1]["limit"], 1)

    def test_read_queries_do_not_require_private_coefficients(self):
        from app.api import stage2, stage5
        queries = [stage2._asset_query("grid_cells"), stage2._asset_stage4_public_filtered_query("grid_cells"), stage5._export_rows_sql(include_geometry=False)]
        for query in queries:
            sql = str(query)
            self.assertNotIn(":stage5_", sql)
            self.assertNotIn(":stage5_stoichiometric_factor", sql)
        self.assertIn("to_jsonb", stage2._stage5_net_annual_increment_ha_sql())
        self.assertIn("jsonb_build_object", stage2._stage5_year_over_year_fallback_sql())

    def test_original_migration_graph_is_intact(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        scripts = ScriptDirectory.from_config(Config(str(ROOT / "backend/alembic.ini")))
        revisions = list(scripts.walk_revisions())
        self.assertEqual(len(revisions), 47)
        self.assertEqual(len(scripts.get_heads()), 1)

    def test_private_producers_cannot_start(self):
        for module in ["app.worker", "app.tasks.stage4"]:
            with self.subTest(module=module), self.assertRaisesRegex(RuntimeError, "private|Private"):
                runpy.run_module(module, run_name="producer_boundary_check")

    def test_private_calculation_command_is_unavailable(self):
        import importlib
        module = importlib.import_module("app.commands.run_stage1_verification")
        with self.assertRaisesRegex(RuntimeError, "Private"):
            module.main()

    def test_actual_http_contracts_remain_present(self):
        inventory = json.loads((ROOT / "docs/contracts/route-inventory.json").read_text())
        schema = app.openapi()
        for route in inventory["routes"]:
            self.assertIn(route["method"].lower(), schema["paths"][route["path"]])
        self.assertEqual(self.call("GET", "/stage2/assets/not-a-uuid")[0], 422)


if __name__ == "__main__": unittest.main()
