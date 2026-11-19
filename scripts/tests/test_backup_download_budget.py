import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("backup_download_budget", Path(__file__).parents[1] / "backup_offsite.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
KEY = "map1/daily/20261103T000000Z-" + "a" * 32 + "/COMPLETE.json"


class DownloadBudgetTest(unittest.TestCase):
    def setUp(self):
        space = patch.object(backup.os, "statvfs", return_value=SimpleNamespace(
            f_bavail=1024**4, f_frsize=1, f_favail=1_000_000))
        space.start()
        self.addCleanup(space.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.destination = self.root / "restored"
        self.body = b"encrypted fixture"
        self.manifest = {"files": [{"name": "database.dump.age", "size": len(self.body),
                                    "sha256": hashlib.sha256(self.body).hexdigest()}]}
        self.client = Mock()
        self.read = patch.object(backup, "read_manifest", return_value=self.manifest)
        self.verify = patch.object(backup, "verify_remote")
        self.read.start(); self.verify.start()
        self.addCleanup(self.read.stop); self.addCleanup(self.verify.stop); self.addCleanup(self.tmp.cleanup)

    def run_download(self, **kw):
        return backup.download_bundle(self.client, "fixture", "map1", KEY, self.destination, **kw)

    def test_declared_size_cap_rejects_before_download(self):
        with self.assertRaisesRegex(ValueError, "size cap"):
            self.run_download(max_total_bytes=len(self.body)-1)
        self.client.download_file.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_disk_and_inode_reserve_fail_before_download(self):
        for free_bytes, free_inodes in ((1, 1_000_000), (10**12, 0)):
            with self.subTest(free_bytes=free_bytes), patch.object(backup.os, "statvfs", return_value=SimpleNamespace(
                    f_bavail=free_bytes, f_frsize=1, f_favail=free_inodes)):
                with self.assertRaisesRegex(ValueError, "Insufficient"):
                    self.run_download()
        self.client.download_file.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_competing_writer_aborts_transfer_and_removes_partial_files(self):
        def download(_bucket, _key, destination, **options):
            Path(destination).write_bytes(self.body)
            with patch.object(backup.os, "statvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=1, f_favail=1_000_000)):
                options["Callback"](len(self.body))
        self.client.download_file.side_effect = download
        with self.assertRaisesRegex(ValueError, "Insufficient space"):
            self.run_download(min_free_bytes=1)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_provider_growth_cannot_bypass_transfer_cap(self):
        def download(_bucket, _key, destination, **options):
            Path(destination).write_bytes(self.body+b"extra")
            options["Callback"](len(self.body)+5)
        self.client.download_file.side_effect = download
        with self.assertRaisesRegex(ValueError, "transfer exceeded"):
            self.run_download(max_total_bytes=len(self.body))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_retry_progress_and_complete_valid_download(self):
        def download(_bucket, _key, destination, **options):
            callback=options["Callback"]
            callback(5); callback(-5)
            Path(destination).write_bytes(self.body)
            callback(len(self.body))
        self.client.download_file.side_effect=download
        result=self.run_download(max_total_bytes=len(self.body))
        self.assertEqual(result["status"], "downloaded_and_checksum_verified")
        self.assertEqual((self.destination/"database.dump.age").read_bytes(), self.body)
        self.assertTrue((self.destination/"database.dump.age.sha256").is_file())

    def test_invalid_limits_and_symlink_destination_are_rejected(self):
        for kwargs in ({"max_total_bytes":0},{"max_total_bytes":True},{"min_free_bytes":-1},{"min_free_inodes":-1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_download(**kwargs)
        self.destination.symlink_to(self.root/"does-not-exist")
        with self.assertRaises(ValueError):
            self.run_download()
        self.client.download_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
