"""Run the real scanner in disposable Git repositories; no network or Docker."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCANNER = Path(__file__).resolve().parents[1] / "check-tracked-secrets.sh"


def fixture_url(scheme="https", username="<USER>", value="<PASSWORD>", host="example.test", suffix="/"):
    return "".join((scheme, "://", username, ":", value, "@", host, suffix))


class SecretScanTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="map1-secret-scan-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = dict(os.environ, CHECK_LOCAL_SECRETS="0", PYTHONDONTWRITEBYTECODE="1")
        # Do not let caller Git environment redirect operations outside the fixture.
        for key in list(self.env):
            if key.startswith("GIT_"):
                del self.env[key]
        subprocess.run(["git", "init", "-q", str(self.root)], env=self.env, check=True, capture_output=True)

    def write(self, path, content, *, tracked=True):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if tracked:
            subprocess.run(["git", "add", "--", path], cwd=self.root, env=self.env,
                           check=True, capture_output=True)

    def scan(self, *, local=False, path=None):
        env = dict(self.env, CHECK_LOCAL_SECRETS="1" if local else "0")
        if path is not None:
            env["PATH"] = str(path)
        return subprocess.run(["/bin/sh", str(SCANNER)], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=20)

    def assert_flagged(self, result, path, *private_values):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(path, result.stdout)
        for value in private_values:
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_contextvar_result_is_not_a_plain_credential(self):
        self.write("backend/app/services/stage4_automation.py",
                   "token = _stage4_batch_lease.set(lease)\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_call_is_not_executed(self):
        self.write("application.py", "raise RuntimeError('scanner must not import me')\n"
                   "token = context_variable.set(runtime_value)\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_call_containing_literal_is_not_exempted(self):
        value = "SCAN_CANARY_" + "X" * 20
        self.write("application.py", "token = context_variable.set(" + repr(value) + ")\n")
        self.assert_flagged(self.scan(), "application.py", value)

    def test_call_does_not_hide_a_second_statement(self):
        value = "SCAN_CANARY_" + "Y" * 20
        self.write("application.py", "token = context_variable.set(runtime_value); password = " + repr(value) + "\n")
        self.assert_flagged(self.scan(), "application.py", value)

    def test_real_assignment_still_flags_same_file(self):
        value = "SCAN_CANARY_" + "Z" * 20
        self.write("backend/app/services/stage4_automation.py",
                   "token = _stage4_batch_lease.set(lease)\npassword = " + repr(value) + "\n")
        self.assert_flagged(self.scan(), "backend/app/services/stage4_automation.py", value)

    def test_plain_environment_and_quoted_assignments_remain_detected(self):
        value = "SCAN_CANARY_" + "W" * 20
        for name, text in [("runtime.conf", "APP_PASSWORD=" + value + "\n"),
                           ("application.py", "password = " + repr(value) + "\n")]:
            with self.subTest(name=name):
                self.write(name, text)
                self.assert_flagged(self.scan(), name, value)

    def test_both_exact_dummy_urls_are_exempted(self):
        self.write("scripts/tests/test_backup_alert.py", "endpoint = " + repr(fixture_url()) + "\n")
        proxy = fixture_url("http", "<USER>", "<PASSWORD>", "localhost:9", "")
        self.write("scripts/tests/test_stage3_host_network.py", "endpoint = " + repr(proxy) + "\n")
        for local in (False, True):
            with self.subTest(local=local):
                result = self.scan(local=local)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dummy_url_in_other_file_is_still_reported(self):
        value = fixture_url()
        self.write("application.py", "endpoint = " + repr(value) + "\n")
        self.assert_flagged(self.scan(), "application.py", value)

    def test_changed_credentials_or_host_in_allowlisted_test_are_reported(self):
        path = "scripts/tests/test_backup_alert.py"
        values = [fixture_url(value="SCAN_CANARY_" + "V" * 20),
                  fixture_url(host="other.example.test"), fixture_url(suffix="/other")]
        for value in values:
            with self.subTest(kind=values.index(value)):
                self.write(path, "endpoint = " + repr(value) + "\n")
                self.assert_flagged(self.scan(), path, value)

    def test_same_line_second_credential_is_not_hidden_by_fixture(self):
        path = "scripts/tests/test_backup_alert.py"
        other = fixture_url(value="SCAN_CANARY_" + "U" * 20)
        self.write(path, "endpoints = (" + repr(fixture_url()) + ", " + repr(other) + ")\n")
        self.assert_flagged(self.scan(), path, other)

    def test_parse_failure_is_not_exempted(self):
        path = "scripts/tests/test_backup_alert.py"
        self.write(path, "invalid python!\nendpoint = " + repr(fixture_url()) + "\n")
        self.assert_flagged(self.scan(), path, fixture_url())

    def test_nonliteral_fixture_comment_is_not_exempted(self):
        path = "scripts/tests/test_backup_alert.py"
        self.write(path, "# endpoint " + fixture_url() + "\n")
        self.assert_flagged(self.scan(), path, fixture_url())

    def test_known_token_signature_remains_independent(self):
        value = "gh" + "p_" + "A" * 36
        self.write("application.py", "token = context_variable.set(" + repr(value) + ")\n")
        result = self.scan()
        self.assert_flagged(result, "application.py", value)
        self.assertIn("token/private-key", result.stdout)

    def test_untracked_local_secret_is_reported(self):
        value = fixture_url(value="SCAN_CANARY_" + "T" * 20)
        self.write("untracked.py", "endpoint = " + repr(value) + "\n", tracked=False)
        self.assertEqual(self.scan().returncode, 0)
        self.assert_flagged(self.scan(local=True), "untracked.py", value)

    def test_missing_filter_helper_fails_closed(self):
        isolated = self.root / "scanner.sh"
        isolated.write_text(SCANNER.read_text())
        result = subprocess.run(["/bin/sh", str(isolated)], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("filter-secret-scan-candidates.py", result.stderr)

    def test_filter_does_not_follow_symlink_for_fixture_exemption(self):
        self.write("source.py", "endpoint = " + repr(fixture_url()) + "\n")
        link = self.root / "scripts/tests/test_backup_alert.py"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root / "source.py")
        pattern = r"://[^/@:[:space:]]+:[^/@[:space:]]+@"
        result = subprocess.run([shutil.which("python3"), str(SCANNER.with_name(
            "filter-secret-scan-candidates.py")), "credential URL", pattern],
            input="scripts/tests/test_backup_alert.py\n", cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "scripts/tests/test_backup_alert.py\n")

    def test_find_grep_fallback_preserves_filtering_and_detection(self):
        binary = self.root / "bin"
        binary.mkdir()
        for name in ("git", "sed", "cat", "python3", "find", "grep", "awk", "dirname"):
            (binary / name).symlink_to(shutil.which(name))
        self.write("scripts/tests/test_backup_alert.py", "endpoint = " + repr(fixture_url()) + "\n")
        self.write("application.py", "token = context_variable.set(runtime_value)\n")
        result = self.scan(local=True, path=binary)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = fixture_url(value="SCAN_CANARY_" + "S" * 20)
        self.write("untracked.py", "endpoint = " + repr(value) + "\n", tracked=False)
        self.assert_flagged(self.scan(local=True, path=binary), "untracked.py", value)


if __name__ == "__main__":
    unittest.main()
