import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("showcase_scan", ROOT / "scripts/check_showcase.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class SourceScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_root = scanner.ROOT
        scanner.ROOT = self.root

    def tearDown(self):
        scanner.ROOT = self.previous_root
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})

    def initialize(self):
        self.git("init", "-q")

    def canary(self):
        return "gh" + "p_" + "Z" * 32

    def test_git_metadata_does_not_fail_a_normal_checkout(self):
        self.initialize()
        (self.root / "README.md").write_text("Architecture review source.\n")
        self.git("add", "README.md")
        report = scanner.scan()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["files"], ["README.md"])

    def test_tracked_source_token_is_rejected(self):
        self.initialize()
        (self.root / "source.py").write_text(repr(self.canary()))
        self.git("add", "source.py")
        self.assertIn("access_token", {x["category"] for x in scanner.scan()["findings"]})

    def test_force_tracked_ignored_artifact_is_not_exempt(self):
        self.initialize()
        (self.root / ".gitignore").write_text(".local-checks/\n")
        (self.root / ".local-checks").mkdir()
        (self.root / ".local-checks/leak.txt").write_text(self.canary())
        self.git("add", ".gitignore")
        self.git("add", "-f", ".local-checks/leak.txt")
        categories = {x["category"] for x in scanner.scan()["findings"]}
        self.assertIn("access_token", categories)
        self.assertIn("forbidden_artifact", categories)

    def test_nonignored_untracked_source_is_checked(self):
        self.initialize()
        (self.root / "new.py").write_text(repr(self.canary()))
        self.assertEqual(scanner.scan()["status"], "fail")

    def test_symlink_to_external_content_is_rejected(self):
        self.initialize()
        external = self.root.parent / (self.root.name + "-outside")
        try:
            external.write_text("External content.")
            (self.root / "linked.py").symlink_to(external)
            self.git("add", "linked.py")
            self.assertIn("path_outside_source", {x["category"] for x in scanner.scan()["findings"]})
        finally:
            external.unlink(missing_ok=True)

    def test_stale_scope_projection_is_rejected(self):
        self.initialize()
        (self.root / "source.txt").write_text("Current source.")
        (self.root / "docs").mkdir()
        (self.root / "docs/scope-projection.json").write_text(json.dumps({
            "file_decisions": [{"path": "source.txt", "sha256": "0" * 64}]
        }))
        self.git("add", ".")
        self.assertIn("projection_hash_mismatch", {x["category"] for x in scanner.scan()["findings"]})

    def test_scope_projection_cannot_hash_itself(self):
        self.initialize()
        (self.root / "docs").mkdir()
        (self.root / "docs/scope-projection.json").write_text(json.dumps({
            "file_decisions": [{"path": "docs/scope-projection.json", "sha256": "0" * 64}]
        }))
        self.git("add", ".")
        self.assertIn("projection_self_hash", {x["category"] for x in scanner.scan()["findings"]})

    def test_plain_source_archive_is_supported(self):
        (self.root / "README.md").write_text("Architecture review source.\n")
        self.assertEqual(scanner.scan()["status"], "pass")


if __name__ == "__main__":
    unittest.main()
