"""Local guard regressions; fake mount evidence/tools, no server/provider calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import backup_volume_guard as guard
import backup_budget as budget


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".volume-guard-", dir=SCRIPTS / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.volume = self.root / "volume"
        self.volume.mkdir()
        self.dest = self.volume / "backups"
        self.identity = "01234567-0123-4567-89ab-0123456789ab"
        self.env = {"BACKUP_VOLUME_MOUNT": str(self.volume), "BACKUP_VOLUME_UUID": self.identity,
                    "BACKUP_EXTRA_RESERVE_PATH": "/", "BACKUP_MIN_FREE_GB": "40",
                    "BACKUP_MIN_FREE_INODES": "100000", "BACKUP_MAX_DUMP_GB": "1"}
        self.config = guard.settings(environ=self.env)
        self.mount_uuid = self.identity
        self.root_free = self.volume_free = 100 * guard.GIB
        self.root_inodes = self.volume_inodes = 1_000_000
        real_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            value = real_stat(path, *args, **kwargs)
            if path.is_relative_to(self.volume):
                parts = list(value); parts[2] += 1
                return os.stat_result(parts)
            return value
        def mount_result(*args, **kwargs):
            data = {"filesystems": [{"target": str(self.volume), "uuid": self.mount_uuid, "options": "rw"}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(data))
        def free(path):
            root = Path(path) == Path("/")
            return SimpleNamespace(f_bavail=self.root_free if root else self.volume_free, f_frsize=1,
                                   f_favail=self.root_inodes if root else self.volume_inodes)
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch.object(Path, "stat", fake_stat))
        stack.enter_context(patch.object(guard.os.path, "ismount", return_value=True))
        stack.enter_context(patch.object(guard.subprocess, "run", side_effect=mount_result))
        stack.enter_context(patch.object(guard.os, "statvfs", side_effect=free))

    def test_valid_config_allows_missing_child_without_creating_it(self):
        guard.check(self.config, self.dest)
        self.assertFalse(self.dest.exists())

    def test_missing_mount_refused_before_destination_creation(self):
        with patch.object(guard.os.path, "ismount", return_value=False):
            with self.assertRaises(guard.GuardError):
                guard.check(self.config, self.dest)
        self.assertFalse(self.dest.exists())

    def test_wrong_uuid_refused(self):
        self.mount_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        with self.assertRaisesRegex(guard.GuardError, "UUID"):
            guard.check(self.config, self.dest)

    def test_root_and_volume_byte_and_inode_floors(self):
        for field, value in (("root_free", 40*guard.GIB-1), ("volume_free", 40*guard.GIB-1),
                             ("root_inodes", 99999), ("volume_inodes", 99999)):
            with self.subTest(field=field):
                old = getattr(self, field); setattr(self, field, value)
                with self.assertRaises(guard.GuardError):
                    guard.check(self.config, self.dest)
                setattr(self, field, old)

    def test_path_outside_volume_and_symlink_refused(self):
        with self.assertRaises(guard.GuardError):
            guard.check(self.config, self.root / "outside")
        link = self.volume / "link"; link.symlink_to(self.root)
        with self.assertRaises(guard.GuardError):
            guard.check(self.config, link / "backup")

    def test_identity_rechecked_during_writer_and_owned_child_stopped(self):
        self.dest.mkdir()
        marker = self.root / "writer-pid"
        code = "import os,time;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(20)"
        def watch():
            if marker.exists():
                self.mount_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
            guard.check(self.config, self.dest)
        with contextlib.redirect_stderr(io.StringIO()):
            status = budget.run_guard([sys.executable, "-B", "-c", code], directory=self.dest,
                                      min_free_bytes=0, min_free_inodes=0, max_file_bytes=1024**2,
                                      storage_check=watch, interval_seconds=.02, grace_seconds=.1)
        self.assertEqual(status, 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_root_breach_stops_writer_continuously(self):
        self.dest.mkdir()
        marker = self.root / "writer-pid"
        code = "import os,time;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(20)"
        def watch():
            if marker.exists():
                self.root_free = 40*guard.GIB-1
            guard.check(self.config, self.dest)
        with contextlib.redirect_stderr(io.StringIO()):
            status = budget.run_guard([sys.executable, "-B", "-c", code], directory=self.dest,
                                      min_free_bytes=0, min_free_inodes=0, max_file_bytes=1024**2,
                                      storage_check=watch, interval_seconds=.02, grace_seconds=.1)
        self.assertEqual(status, 1)


class SettingsAndEntrypoints(unittest.TestCase):
    def test_unconfigured_legacy_and_incomplete_guard(self):
        self.assertIsNone(guard.settings(environ={}))
        for env in ({"BACKUP_VOLUME_UUID": "x"}, {"BACKUP_VOLUME_MOUNT": "/volume"},
                    {"BACKUP_EXTRA_RESERVE_PATH": "/"}):
            with self.assertRaises(guard.GuardError):
                guard.settings(environ=env)

    def test_runtime_file_supplies_manual_guard_and_no_secret_output(self):
        with tempfile.TemporaryDirectory(dir=SCRIPTS / "tests") as temp:
            path = Path(temp) / "runtime.env"
            path.write_text("UNRELATED_SECRET=must-not-print\nBACKUP_VOLUME_MOUNT=/volume\n"
                            "BACKUP_VOLUME_UUID=01234567-0123-4567-89ab-0123456789ab\n"
                            "BACKUP_EXTRA_RESERVE_PATH=/\nBACKUP_MAX_DUMP_GB=64\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                config = guard.settings(path, environ={})
            self.assertEqual(str(config["extra"]), "/")
            self.assertEqual(out.getvalue(), "")
            path.write_text(path.read_text() + "BACKUP_VOLUME_MOUNT=/other\n")
            with self.assertRaises(guard.GuardError):
                guard.settings(path, environ={})

    def test_manual_backup_refuses_before_mkdir_or_dump(self):
        import test_backup_scripts as workflow
        fixture = workflow.BackupScriptsTests()
        fixture.setUp()
        try:
            volume = fixture.root / "volume"; volume.mkdir()
            target = volume / "backups"
            result = fixture.run_script("backup-db.sh", BACKUP_DIR=str(target),
                BACKUP_VOLUME_MOUNT=str(volume), BACKUP_VOLUME_UUID="01234567-0123-4567-89ab-0123456789ab",
                BACKUP_EXTRA_RESERVE_PATH="/", BACKUP_MAX_DUMP_GB="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertNotIn("dump", fixture.actions())
        finally:
            fixture.doCleanups()

    def test_manual_writer_refusal_never_publishes_encrypted_or_plaintext_backup(self):
        import test_backup_scripts as workflow
        # The real shell entrypoint must propagate its guard's later refusal,
        # clean partial staging, and emit no successful archive path. Actual
        # mount/UUID/reserve enforcement is exercised by GuardTests above.
        for encrypted in (True, False):
            with self.subTest(encrypted=encrypted):
                fixture = workflow.BackupScriptsTests()
                fixture.setUp()
                try:
                    scripts = fixture.root / 'operators'
                    scripts.mkdir()
                    script = scripts / 'backup-db.sh'
                    script.write_bytes((SCRIPTS / 'backup-db.sh').read_bytes())
                    (scripts / 'backup_volume_guard.py').write_text(
                        "import json,os,sys\nfrom pathlib import Path\n"
                        "writer='--writer' in sys.argv\n"
                        "with (Path(os.environ['FAKE_ROOT'])/'calls.jsonl').open('a') as log:\n"
                        " log.write(json.dumps({'action':'guard_writer' if writer else 'guard_admission'})+'\\n')\n"
                        "raise SystemExit(23 if writer else 0)\n")
                    changes = {} if encrypted else {
                        'BACKUP_AGE_RECIPIENT': '', 'BACKUP_REQUIRE_ENCRYPTION': '0',
                        'ALLOW_PLAINTEXT_BACKUP': '1'}
                    result = fixture.run_script(str(script), **changes)
                    fixture.assert_failed_cleanly(result)
                    self.assertEqual(fixture.actions().count('guard_admission'), 1)
                    self.assertEqual(fixture.actions().count('guard_writer'), 1)
                    self.assertNotIn('encrypt', fixture.actions())
                finally:
                    fixture.doCleanups()

    def test_scheduled_entrypoint_refuses_before_mkdir_or_provider(self):
        import test_backup_offsite_job as workflow
        fixture = workflow.OffsiteJobTest()
        fixture.setUp()
        try:
            volume = fixture.root / "volume"; volume.mkdir()
            target = volume / "backups"
            result = fixture.run_job(BACKUP_DIR=str(target), BACKUP_MIN_FREE_GB="40",
                BACKUP_VOLUME_MOUNT=str(volume), BACKUP_VOLUME_UUID="01234567-0123-4567-89ab-0123456789ab",
                BACKUP_EXTRA_RESERVE_PATH="/")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertFalse((fixture.root / "calls").exists())
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
