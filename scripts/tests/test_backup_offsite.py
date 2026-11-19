import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import boto3
from moto import mock_aws


spec = importlib.util.spec_from_file_location("backup_offsite", Path(__file__).parents[1] / "backup_offsite.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@mock_aws
class OffsiteBackupTest(unittest.TestCase):
    def setUp(self):
        space = patch.object(backup.os, "statvfs", return_value=SimpleNamespace(
            f_bavail=1024**4, f_frsize=1, f_favail=1_000_000))
        space.start()
        self.addCleanup(space.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "map1-turkey-20261029-120000.dump.age").write_bytes(b"age-encryption.org/v1\nexample encrypted database")
        (self.bundle / "configuration.tar.age").write_bytes(b"age-encryption.org/v1\nexample encrypted configuration")
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        self.client.create_bucket(Bucket="map1-backup-test")

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, now=None):
        return backup.upload_bundle(self.client, "map1-backup-test", "map1", self.bundle, now)

    def test_upload_and_download_round_trip(self):
        key = self.upload()["completion_keys"][0]
        destination = self.root / "downloaded"
        result = backup.download_bundle(self.client, "map1-backup-test", "map1", key, destination)
        self.assertEqual(result["archives"], 2)
        for source in self.bundle.iterdir():
            self.assertEqual(source.read_bytes(), (destination / source.name).read_bytes())
            self.assertTrue((destination / (source.name + ".sha256")).is_file())

    def test_monday_creates_independent_weekly_copy(self):
        result = self.upload(datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(len(result["completion_keys"]), 2)
        self.assertIn("/weekly/", result["completion_keys"][1])

    def test_partial_upload_has_no_complete_marker(self):
        original = self.client.upload_file
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("transfer interrupted")
            return original(*args, **kwargs)
        with patch.object(self.client, "upload_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.upload()
        keys = [item["Key"] for item in self.client.list_objects_v2(Bucket="map1-backup-test").get("Contents", [])]
        self.assertFalse(any(key.endswith("COMPLETE.json") for key in keys))

    def test_mismatched_remote_metadata_never_commits(self):
        with patch.object(self.client, "head_object", return_value={"ContentLength": 1, "Metadata": {}}):
            with self.assertRaises(ValueError):
                self.upload()
        self.assertFalse(any(item["Key"].endswith("COMPLETE.json") for item in self.client.list_objects_v2(Bucket="map1-backup-test")["Contents"]))

    def test_same_size_remote_corruption_never_commits(self):
        original = self.client.upload_file
        def corrupt_upload(filename, bucket, key, **kwargs):
            original(filename, bucket, key, **kwargs)
            path = Path(filename)
            self.client.put_object(Bucket=bucket, Key=key, Body=b"!" * path.stat().st_size,
                                   Metadata={"sha256": backup.digest(path)})
        with patch.object(self.client, "upload_file", side_effect=corrupt_upload):
            with self.assertRaises(ValueError):
                self.upload()
        keys = [item["Key"] for item in self.client.list_objects_v2(Bucket="map1-backup-test")["Contents"]]
        self.assertFalse(any(key.endswith("/COMPLETE.json") for key in keys))

    def test_retrying_old_snapshot_does_not_reset_freshness(self):
        (self.bundle / "snapshot-at.txt").write_text((datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        self.upload()
        with self.assertRaises(ValueError):
            backup.check_freshness(self.client, "map1-backup-test", "map1", 30)

    def test_old_retry_does_not_shadow_a_newer_valid_snapshot(self):
        (self.bundle / "snapshot-at.txt").write_text((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        fresh_key = self.upload()["completion_keys"][0]
        (self.bundle / "snapshot-at.txt").write_text((datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        self.upload()
        result = backup.check_freshness(self.client, "map1-backup-test", "map1", 30)
        self.assertEqual(result["completion_key"], fresh_key)

    def test_corrupt_remote_content_is_rejected_and_not_published_locally(self):
        key = self.upload()["completion_keys"][0]
        base = key.rsplit("/", 1)[0]
        path = self.bundle / "configuration.tar.age"
        corrupt = b"!" * path.stat().st_size
        self.client.put_object(Bucket="map1-backup-test", Key=base + "/" + path.name, Body=corrupt,
                               Metadata={"sha256": backup.digest(path)})
        destination = self.root / "corrupt"
        with self.assertRaises(ValueError):
            backup.download_bundle(self.client, "map1-backup-test", "map1", key, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".restore-*")))

    def test_existing_restore_directory_is_never_overwritten(self):
        key = self.upload()["completion_keys"][0]
        destination = self.root / "existing"
        destination.mkdir()
        with self.assertRaises(ValueError):
            backup.download_bundle(self.client, "map1-backup-test", "map1", key, destination)

    def test_traversal_manifest_is_rejected(self):
        manifest = {"format": 1, "files": [
            {"name": "../escape.age", "size": 1, "sha256": "a" * 64},
            {"name": "configuration.tar.age", "size": 1, "sha256": "b" * 64}]}
        with self.assertRaises(ValueError):
            backup.validate_manifest(manifest)

    def test_symlink_archive_is_rejected(self):
        (self.bundle / "external.age").symlink_to(self.bundle / "configuration.tar.age")
        with self.assertRaises(ValueError):
            self.upload()

    def test_plaintext_renamed_as_encrypted_is_rejected(self):
        (self.bundle / "configuration.tar.age").write_bytes(b"plain text containing private configuration")
        with self.assertRaises(ValueError):
            self.upload()

    def test_freshness_requires_complete_set_and_rejects_stale(self):
        key = self.upload()["completion_keys"][0]
        result = backup.check_freshness(self.client, "map1-backup-test", "map1", 30)
        self.assertEqual(result["status"], "fresh")
        with self.assertRaises(ValueError):
            backup.check_freshness(self.client, "map1-backup-test", "map1", 30,
                                   datetime.now(timezone.utc) + timedelta(hours=31))
        self.client.delete_object(Bucket="map1-backup-test", Key=key.rsplit("/", 1)[0] + "/configuration.tar.age")
        with self.assertRaises(Exception):
            backup.check_freshness(self.client, "map1-backup-test", "map1", 30)

    def test_missing_credentials_do_not_fall_back_to_personal_accounts(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                backup.settings()

    def test_remote_plain_http_is_forbidden_even_in_test_mode(self):
        env = {"BACKUP_S3_" + key: value for key, value in {
            "ENDPOINT_URL": "http://storage.example.com", "REGION": "test", "BUCKET": "test",
            "ACCESS_KEY_ID": "test", "SECRET_ACCESS_KEY": "test"}.items()}
        env["BACKUP_S3_LOCAL_TEST"] = "1"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                backup.settings()

    def test_completion_key_cannot_escape_project_prefix(self):
        with self.assertRaises(ValueError):
            backup.validate_key("map1", "another-project/daily/COMPLETE.json")


if __name__ == "__main__":
    unittest.main()
