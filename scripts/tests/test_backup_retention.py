"""Object Lock regressions using isolated S3 emulation and deliberately faulty responses."""
import importlib.util
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from moto import mock_aws


spec = importlib.util.spec_from_file_location("backup_retention_subject", Path(__file__).parents[1] / "backup_offsite.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@mock_aws
class BackupRetentionTest(unittest.TestCase):
    def setUp(self):
        space = patch.object(backup.os, "statvfs", return_value=SimpleNamespace(
            f_bavail=1024**4, f_frsize=1, f_favail=1_000_000))
        space.start()
        self.addCleanup(space.stop)
        self.environment = patch.dict(os.environ, {"BACKUP_S3_OBJECT_LOCK_MODE": "COMPLIANCE", "BACKUP_S3_PROVIDER": "s3"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "map1-20261030.dump.age").write_bytes(b"age-encryption.org/v1\nfixture database ciphertext")
        (self.bundle / "configuration.tar.age").write_bytes(b"age-encryption.org/v1\nfixture configuration ciphertext")
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        self.bucket = "map1-retention-test"
        self.client.create_bucket(Bucket=self.bucket, ObjectLockEnabledForBucket=True)

    def upload(self, now=None):
        return backup.upload_bundle(self.client, self.bucket, "map1", self.bundle, now=now)

    def current_manifest(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        try:
            return json.loads(response["Body"].read())
        finally:
            response["Body"].close()

    def assert_no_completion_marker(self):
        keys = [v["Key"] for v in self.client.list_objects_v2(Bucket=self.bucket).get("Contents", [])]
        self.assertFalse(any(key.endswith("/COMPLETE.json") for key in keys))

    def test_daily_and_weekly_lock_every_object_and_record_exact_versions(self):
        now = datetime.now(timezone.utc)
        monday = now + timedelta(days=(-now.weekday()) % 7)
        with patch.object(self.client, "put_object", wraps=self.client.put_object) as put:
            result = self.upload(monday)
        self.assertEqual(result["object_lock_mode"], "COMPLIANCE")
        self.assertEqual(len(result["completion_keys"]), 2)
        versions = {v["key"]: v["version_id"] for v in result["completion_versions"]}
        marker_calls = [c.kwargs for c in put.call_args_list if c.kwargs["Key"].endswith("/COMPLETE.json")]
        self.assertEqual(len(marker_calls), 2)
        self.assertTrue(all(c["IfNoneMatch"] == "*" for c in marker_calls))
        for key in result["completion_keys"]:
            days = 7 if "/daily/" in key else 28
            deadline = monday.replace(microsecond=0) + timedelta(days=days)
            manifest = self.current_manifest(key)
            self.assertEqual(datetime.fromisoformat(manifest["object_lock"]["retain_until"]), deadline)
            objects = [(key, versions[key])]
            objects.extend((key.rsplit("/", 1)[0] + "/" + item["name"], item["version_id"])
                           for item in manifest["files"])
            for object_key, version_id in objects:
                self.assertNotIn(version_id, (None, "", "null"))
                head = self.client.head_object(Bucket=self.bucket, Key=object_key, VersionId=version_id)
                self.assertEqual(head["VersionId"], version_id)
                self.assertEqual(head["ObjectLockMode"], "COMPLIANCE")
                self.assertEqual(head["ObjectLockRetainUntilDate"], deadline)

    def test_multipart_lock_request_and_confirmed_retention_response(self):
        # Moto also returns an incorrect multipart CRC. Keep our mandatory full-GET
        # SHA-256 verification, while disabling this optional SDK checksum in this fixture.
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test",
                                   config=Config(response_checksum_validation="when_required"))
        (self.bundle / "map1-20261030.dump.age").write_bytes(b"age-encryption.org/v1\n" + b"x" * (6 * 1024**2))
        transfer = TransferConfig(multipart_threshold=1024**2, multipart_chunksize=5 * 1024**2, max_concurrency=1)
        original = self.client._make_api_call
        requested_locks = {}
        def confirmed_provider(operation, parameters):
            response = original(operation, parameters)
            pair = (parameters.get("Bucket"), parameters.get("Key"))
            if operation == "CreateMultipartUpload":
                self.assertEqual(parameters["ObjectLockMode"], "COMPLIANCE")
                requested_locks[pair] = {field: parameters[field] for field in
                                         ("ObjectLockMode", "ObjectLockRetainUntilDate")}
            elif operation == "HeadObject" and pair in requested_locks:
                # Moto 5.1.17 drops Object Lock at multipart completion. Stub only that
                # provider response after asserting the SDK actually sent the lock request.
                response.update(requested_locks[pair])
            return response
        with patch.object(backup, "TRANSFER", transfer), patch.object(self.client, "_make_api_call", side_effect=confirmed_provider):
            result = self.upload()
            self.assertTrue(requested_locks)
            for key in result["completion_keys"]:
                manifest = self.current_manifest(key)
                item = next(v for v in manifest["files"] if v["name"].endswith(".dump.age"))
                head = self.client.head_object(Bucket=self.bucket, Key=key.rsplit("/", 1)[0] + "/" + item["name"],
                                               VersionId=item["version_id"])
                self.assertIn("-", head["ETag"])
                self.assertEqual(head["ObjectLockMode"], "COMPLIANCE")

    def test_multipart_without_confirmed_retention_fails_closed(self):
        (self.bundle / "map1-20261030.dump.age").write_bytes(b"age-encryption.org/v1\n" + b"x" * (6 * 1024**2))
        transfer = TransferConfig(multipart_threshold=1024**2, multipart_chunksize=5 * 1024**2, max_concurrency=1)
        original = self.client.head_object
        def missing_retention(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"].endswith(".dump.age"):
                response.pop("ObjectLockMode", None)
            return response
        with patch.object(backup, "TRANSFER", transfer), patch.object(self.client, "head_object", side_effect=missing_retention):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                self.upload()
        self.assert_no_completion_marker()

    def test_missing_wrong_short_or_naive_retention_and_missing_versions_fail_before_marker(self):
        original = self.client.head_object
        for failure in ("missing_lock", "governance", "short", "naive", "missing_version", "null_version"):
            with self.subTest(failure=failure):
                def bad_head(**kwargs):
                    response = original(**kwargs)
                    if failure == "missing_lock":
                        response.pop("ObjectLockMode", None)
                        response.pop("ObjectLockRetainUntilDate", None)
                    elif failure == "governance":
                        response["ObjectLockMode"] = "GOVERNANCE"
                    elif failure == "short":
                        response["ObjectLockRetainUntilDate"] -= timedelta(seconds=1)
                    elif failure == "naive":
                        response["ObjectLockRetainUntilDate"] = response["ObjectLockRetainUntilDate"].replace(tzinfo=None)
                    elif failure == "missing_version":
                        response.pop("VersionId", None)
                    else:
                        response["VersionId"] = "null"
                    return response
                with patch.object(self.client, "head_object", side_effect=bad_head):
                    with self.assertRaises(ValueError):
                        self.upload()
                self.assert_no_completion_marker()

    def test_denied_retention_head_fails_before_marker(self):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "fixture retention read denied"}}, "HeadObject")
        with patch.object(self.client, "head_object", side_effect=error):
            with self.assertRaises(ClientError):
                self.upload()
        self.assert_no_completion_marker()

    def test_version_specific_head_must_confirm_requested_archive_version(self):
        original = self.client.head_object
        def bad_head(**kwargs):
            response = original(**kwargs)
            if "VersionId" in kwargs:
                response["VersionId"] = "different-version"
            return response
        with patch.object(self.client, "head_object", side_effect=bad_head):
            with self.assertRaises(ValueError):
                self.upload()
        self.assert_no_completion_marker()

    def test_full_get_sha_rejects_same_size_corruption_despite_valid_lock_and_metadata(self):
        original = self.client.get_object
        def corrupt_get(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"].endswith(".age"):
                self.assertIn("VersionId", kwargs)
                response["Body"].close()
                data = b"!" * response["ContentLength"]
                response["Body"] = StreamingBody(io.BytesIO(data), len(data))
            return response
        with patch.object(self.client, "get_object", side_effect=corrupt_get):
            with self.assertRaisesRegex(ValueError, "integrity"):
                self.upload()
        self.assert_no_completion_marker()

    def test_full_get_must_confirm_requested_version(self):
        original = self.client.get_object
        def wrong_version(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"].endswith(".age"):
                response["VersionId"] = "different-version"
            return response
        with patch.object(self.client, "get_object", side_effect=wrong_version):
            with self.assertRaisesRegex(ValueError, "different object version"):
                self.upload()
        self.assert_no_completion_marker()

    def test_completion_marker_requires_its_own_version_pinned_retention_head(self):
        original = self.client.head_object
        marker_heads = []
        def omit_marker_lock(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"].endswith("/COMPLETE.json"):
                marker_heads.append(kwargs)
                response.pop("ObjectLockMode", None)
            return response
        with patch.object(self.client, "head_object", side_effect=omit_marker_lock):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                self.upload()
        self.assertEqual(len(marker_heads), 1)
        self.assertIn("VersionId", marker_heads[0])

    def test_completion_write_without_version_id_is_not_reported_successful(self):
        original = self.client.put_object
        def omit_version(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"].endswith("/COMPLETE.json"):
                response.pop("VersionId", None)
            return response
        with patch.object(self.client, "put_object", side_effect=omit_version):
            with self.assertRaisesRegex(ValueError, "VersionId"):
                self.upload()

    def test_existing_completion_marker_cannot_be_replaced(self):
        now = datetime.now(timezone.utc)
        with patch.object(backup.uuid, "uuid4") as identifier:
            identifier.return_value.hex = "a" * 32
            first = self.upload(now)
            key = first["completion_keys"][0]
            original_version = self.client.head_object(Bucket=self.bucket, Key=key)["VersionId"]
            with self.assertRaises(ClientError) as raised:
                self.upload(now)
            self.assertEqual(raised.exception.response["Error"]["Code"], "PreconditionFailed")
            self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=key)["VersionId"], original_version)

    def test_declared_retention_shorter_than_group_minimum_is_rejected(self):
        key = self.upload()["completion_keys"][0]
        manifest = self.current_manifest(key)
        manifest["object_lock"]["retain_until"] = (datetime.fromisoformat(manifest["created_at"]) + timedelta(days=6)).isoformat()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(manifest).encode())
        with self.assertRaisesRegex(ValueError, "insufficient retention"):
            backup.read_manifest(self.client, self.bucket, key)

    def test_retained_versions_restore_after_latest_objects_are_overwritten_or_hidden(self):
        result = self.upload()
        key = result["completion_keys"][0]
        marker_version = result["completion_versions"][0]["version_id"]
        manifest = self.current_manifest(key)
        base = key.rsplit("/", 1)[0]
        for item in manifest["files"]:
            # Test administration simulates an external overwrite/delete marker, never uploader operations.
            self.client.put_object(Bucket=self.bucket, Key=base + "/" + item["name"], Body=b"new bad version")
            self.client.delete_object(Bucket=self.bucket, Key=base + "/" + item["name"])
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"invalid new marker")
        self.client.delete_object(Bucket=self.bucket, Key=key)
        destination = self.root / "restored"
        with patch.object(self.client, "download_file", wraps=self.client.download_file) as download:
            restored = backup.download_bundle(self.client, self.bucket, "map1", key, destination, marker_version)
        self.assertEqual(restored["archives"], 2)
        self.assertEqual([c.kwargs["ExtraArgs"]["VersionId"] for c in download.call_args_list],
                         [item["version_id"] for item in manifest["files"]])
        for source in self.bundle.iterdir():
            self.assertEqual(source.read_bytes(), (destination / source.name).read_bytes())

    def test_generic_versioned_bucket_also_pins_archive_versions(self):
        with patch.dict(os.environ, {"BACKUP_S3_OBJECT_LOCK_MODE": ""}):
            result = self.upload()
            key = result["completion_keys"][0]
            manifest = self.current_manifest(key)
            self.assertNotIn("object_lock", manifest)
            for item in manifest["files"]:
                self.assertIn("version_id", item)
                self.client.put_object(Bucket=self.bucket, Key=key.rsplit("/", 1)[0] + "/" + item["name"], Body=b"bad latest")
            backup.download_bundle(self.client, self.bucket, "map1", key, self.root / "generic-restored")

    def test_version_pinned_download_still_checks_sha_and_never_publishes_corrupt_restore(self):
        key = self.upload()["completion_keys"][0]
        destination = self.root / "corrupt-download"
        def corrupt_download(bucket, key, filename, **kwargs):
            self.assertIn("VersionId", kwargs["ExtraArgs"])
            size = self.client.head_object(Bucket=bucket, Key=key, **kwargs["ExtraArgs"])["ContentLength"]
            Path(filename).write_bytes(b"!" * size)
        with patch.object(self.client, "download_file", side_effect=corrupt_download):
            with self.assertRaisesRegex(ValueError, "integrity"):
                backup.download_bundle(self.client, self.bucket, "map1", key, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".restore-*")))

    def test_lock_declarations_are_enforced_even_if_environment_later_disables_lock(self):
        key = self.upload()["completion_keys"][0]
        original = self.client.head_object
        def missing_lock(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"].endswith(".age"):
                response.pop("ObjectLockRetainUntilDate", None)
            return response
        with patch.dict(os.environ, {"BACKUP_S3_OBJECT_LOCK_MODE": ""}):
            with patch.object(self.client, "head_object", side_effect=missing_lock):
                with self.assertRaises(ValueError):
                    backup.download_bundle(self.client, self.bucket, "map1", key, self.root / "bad-lock")
        self.assertFalse((self.root / "bad-lock").exists())

    def test_lock_enabled_rejects_legacy_unlocked_completion(self):
        with patch.dict(os.environ, {"BACKUP_S3_OBJECT_LOCK_MODE": ""}):
            key = self.upload()["completion_keys"][0]
        with self.assertRaisesRegex(ValueError, "does not declare"):
            backup.download_bundle(self.client, self.bucket, "map1", key, self.root / "unlocked")

    def test_missing_or_null_archive_version_is_rejected_before_download(self):
        key = self.upload()["completion_keys"][0]
        manifest = self.current_manifest(key)
        for value in (None, "", "null", "unsafe\nversion"):
            with self.subTest(value=value):
                invalid = json.loads(json.dumps(manifest))
                if value is None:
                    invalid["files"][0].pop("version_id")
                else:
                    invalid["files"][0]["version_id"] = value
                with self.assertRaises(ValueError):
                    backup.validate_manifest(invalid)

    def test_generic_unversioned_defaults_remain_unlocked(self):
        bucket = "map1-generic-default-test"
        self.client.create_bucket(Bucket=bucket)
        with patch.dict(os.environ, {"BACKUP_S3_OBJECT_LOCK_MODE": ""}):
            with patch.object(self.client, "upload_file", wraps=self.client.upload_file) as upload:
                result = backup.upload_bundle(self.client, bucket, "map1", self.bundle)
            self.assertNotIn("completion_versions", result)
            self.assertNotIn("object_lock_mode", result)
            for call in upload.call_args_list:
                self.assertNotIn("ObjectLockMode", call.kwargs["ExtraArgs"])
            backup.download_bundle(self.client, bucket, "map1", result["completion_keys"][0], self.root / "legacy")

    def test_invalid_lock_mode_fails_before_any_upload(self):
        with patch.dict(os.environ, {"BACKUP_S3_OBJECT_LOCK_MODE": "GOVERNANCE"}):
            with patch.object(self.client, "upload_file") as upload:
                with self.assertRaises(ValueError):
                    self.upload()
                upload.assert_not_called()

    def test_runtime_does_not_request_delete_bypass_or_bucket_administration(self):
        original = self.client._make_api_call
        operations = []
        def record(operation, parameters):
            operations.append(operation)
            self.assertNotIn("BypassGovernanceRetention", parameters)
            return original(operation, parameters)
        with patch.object(self.client, "_make_api_call", side_effect=record):
            self.upload()
        self.assertTrue(operations)
        self.assertTrue(set(operations) <= {"PutObject", "HeadObject", "GetObject"}, operations)

    def test_b2_explicitly_uses_retained_version_marker_without_conditional_write(self):
        original = self.client.put_object
        def b2_put(**kwargs):
            if "IfNoneMatch" in kwargs:
                raise ClientError({"Error": {"Code": "NotImplemented"}, "ResponseMetadata": {"HTTPStatusCode": 501}}, "PutObject")
            return original(**kwargs)
        with patch.dict(os.environ, {"BACKUP_S3_PROVIDER": "backblaze-b2"}):
            with patch.object(self.client, "put_object", side_effect=b2_put) as put:
                with patch.object(self.client, "get_object", wraps=self.client.get_object) as get:
                    result = self.upload()
        self.assertEqual(result["provider"], "backblaze-b2")
        self.assertEqual(result["completion_protocol"], "immutable-version")
        for receipt in result["completion_versions"]:
            marker_calls = [c.kwargs for c in put.call_args_list if c.kwargs["Key"] == receipt["key"]]
            self.assertEqual(len(marker_calls), 1)
            self.assertNotIn("IfNoneMatch", marker_calls[0])
            self.assertEqual(marker_calls[0]["ObjectLockMode"], "COMPLIANCE")
            readbacks = [c.kwargs for c in get.call_args_list if c.kwargs["Key"] == receipt["key"]]
            self.assertEqual([c["VersionId"] for c in readbacks], [receipt["version_id"]])

    def test_b2_marker_key_can_change_but_first_receipt_restores_exact_retained_bundle(self):
        original_files = {p.name: p.read_bytes() for p in self.bundle.iterdir()}
        now = datetime.now(timezone.utc)
        with patch.dict(os.environ, {"BACKUP_S3_PROVIDER": "backblaze-b2"}):
            with patch.object(backup.uuid, "uuid4") as identifier:
                identifier.return_value.hex = "b" * 32
                first = self.upload(now)
                (self.bundle / "configuration.tar.age").write_bytes(b"age-encryption.org/v1\nnew configuration ciphertext")
                second = self.upload(now)
            self.assertEqual(first["completion_keys"], second["completion_keys"])
            first_receipt = first["completion_versions"][0]
            self.assertNotEqual(first_receipt["version_id"], second["completion_versions"][0]["version_id"])
            destination = self.root / "first-receipt-recovery"
            backup.download_bundle(self.client, self.bucket, "map1", first_receipt["key"], destination,
                                   manifest_version_id=first_receipt["version_id"])
        for name, data in original_files.items():
            self.assertEqual((destination / name).read_bytes(), data)

    def test_generic_501_does_not_retry_without_condition_or_switch_protocol(self):
        original = self.client.put_object
        marker_calls = []
        def unsupported_condition(**kwargs):
            if kwargs["Key"].endswith("/COMPLETE.json"):
                marker_calls.append(kwargs)
                raise ClientError({"Error": {"Code": "NotImplemented"}, "ResponseMetadata": {"HTTPStatusCode": 501}}, "PutObject")
            return original(**kwargs)
        with patch.object(self.client, "put_object", side_effect=unsupported_condition):
            with self.assertRaises(ClientError):
                self.upload()
        self.assertEqual(len(marker_calls), 1)
        self.assertEqual(marker_calls[0]["IfNoneMatch"], "*")
        self.assert_no_completion_marker()

    def test_b2_requires_compliance_before_client_creation_or_upload(self):
        for mode in ("", "GOVERNANCE"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"BACKUP_S3_PROVIDER": "backblaze-b2", "BACKUP_S3_OBJECT_LOCK_MODE": mode}):
                with patch.object(backup.boto3, "client") as factory, patch.object(self.client, "upload_file") as upload:
                    with self.assertRaises(ValueError):
                        backup.settings()
                    with self.assertRaises(ValueError):
                        self.upload()
                    factory.assert_not_called()
                    upload.assert_not_called()

    def test_unknown_provider_fails_before_client_creation(self):
        with patch.dict(os.environ, {"BACKUP_S3_PROVIDER": "backblaze-typo"}):
            with patch.object(backup.boto3, "client") as factory:
                with self.assertRaises(ValueError):
                    backup.settings()
                factory.assert_not_called()

    def test_b2_transport_removes_expect_on_only_its_dedicated_client(self):
        environment = {"BACKUP_S3_" + k: v for k, v in {
            "ENDPOINT_URL": "https://map1-sdk-fixture.invalid", "REGION": "us-east-1", "BUCKET": self.bucket,
            "ACCESS_KEY_ID": "fixture", "SECRET_ACCESS_KEY": "fixture"}.items()}
        with patch.dict(os.environ, environment):
            generic, _, _ = backup.settings()
            with patch.dict(os.environ, {"BACKUP_S3_PROVIDER": "backblaze-b2"}):
                b2, _, _ = backup.settings()
        class Raw:
            def stream(self, amt=1024, decode_content=False):
                yield b""
        def capture(client):
            captured = {}
            def intercept(request, **kwargs):
                captured.update({k.lower(): v for k, v in request.headers.items()})
                return AWSResponse(request.url, 200, {"ETag": '"fixture"', "x-amz-version-id": "fixture"}, Raw())
            client.meta.events.register("before-send.s3.PutObject", intercept)
            client.put_object(Bucket=self.bucket, Key="transport-fixture", Body=io.BytesIO(b"fixture"), IfNoneMatch="*",
                              ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=datetime.now(timezone.utc) + timedelta(days=7))
            return captured
        self.assertIn("expect", capture(generic))
        b2_headers = capture(b2)
        self.assertNotIn("expect", b2_headers)
        self.assertEqual(b2_headers["if-none-match"], b"*")  # The transport hook strips nothing else.
        self.assertEqual(b2_headers["x-amz-object-lock-mode"], b"COMPLIANCE")
        self.assertIn("x-amz-object-lock-retain-until-date", b2_headers)
        self.assertEqual(b2_headers["content-md5"], base64.b64encode(hashlib.md5(b"fixture", usedforsecurity=False).digest()))
        self.assertNotIn("x-amz-trailer", b2_headers)
        self.assertNotIn("transfer-encoding", b2_headers)
        self.assertNotEqual(b2_headers.get("content-encoding"), b"aws-chunked")
        self.assertIn("expect", capture(generic))

    def test_b2_managed_archives_and_marker_send_md5_without_expect_or_sdk_trailers(self):
        environment = {"BACKUP_S3_" + k: v for k, v in {
            "ENDPOINT_URL": "https://s3.amazonaws.com", "REGION": "us-east-1", "BUCKET": self.bucket,
            "ACCESS_KEY_ID": "fixture", "SECRET_ACCESS_KEY": "fixture", "PROVIDER": "backblaze-b2"}.items()}
        captured = []
        def capture_request(request, **kwargs):
            captured.append((urlsplit(request.url).path.rsplit("/", 1)[-1],
                             {k.lower(): v for k, v in request.headers.items()}))
        with patch.dict(os.environ, environment):
            self.client, _, _ = backup.settings()
            self.client.meta.events.register("before-send.s3.PutObject", capture_request)
            with patch.object(self.client, "put_object", wraps=self.client.put_object) as put:
                result = self.upload()
        markers = {key.rsplit("/", 1)[0]: self.current_manifest(key) for key in result["completion_keys"]}
        self.assertEqual(len(captured), 3 * len(result["completion_keys"]))
        for name, headers in captured:
            self.assertNotIn("expect", headers)
            self.assertNotIn("if-none-match", headers)
            self.assertNotIn("x-amz-trailer", headers)
            self.assertNotIn("transfer-encoding", headers)
            self.assertNotEqual(headers.get("content-encoding"), b"aws-chunked")
            self.assertEqual(headers["x-amz-object-lock-mode"], b"COMPLIANCE")
            if name.endswith(".age"):
                payload = (self.bundle / name).read_bytes()
                self.assertEqual(headers["content-md5"], base64.b64encode(hashlib.md5(payload, usedforsecurity=False).digest()))
        marker_calls = [c.kwargs for c in put.call_args_list if c.kwargs["Key"].endswith("/COMPLETE.json")]
        for call in marker_calls:
            self.assertEqual(call["ContentMD5"], base64.b64encode(hashlib.md5(call["Body"], usedforsecurity=False).digest()).decode("ascii"))
        for manifest in markers.values():
            for item in manifest["files"]:
                self.assertIn("version_id", item)

    def test_content_md5_restores_body_position_in_success_and_read_failure(self):
        body = io.BytesIO(b"prefix-payload")
        body.seek(7)
        expected = base64.b64encode(hashlib.md5(b"payload", usedforsecurity=False).digest()).decode("ascii")
        self.assertEqual(backup.content_md5(body), expected)
        self.assertEqual(body.tell(), 7)
        with patch.object(body, "read", side_effect=OSError("fixture read failure")):
            with self.assertRaises(OSError):
                backup.content_md5(body)
        self.assertEqual(body.tell(), 7)

    def test_nonseekable_b2_body_fails_instead_of_sending_unchecksummed(self):
        class Nonseekable:
            def read(self, size):
                return b"payload"
        with self.assertRaisesRegex(ValueError, "seekable"):
            backup.content_md5(Nonseekable())

    def test_hidden_marker_discovery_and_recovery_without_original_receipt(self):
        with patch.dict(os.environ, {"BACKUP_S3_PROVIDER": "backblaze-b2"}):
            key = self.upload()["completion_keys"][0]
            self.client.delete_object(Bucket=self.bucket, Key=key)
            # Fresh-host discovery uses only provider listing, never an original receipt.
            with patch.object(self.client, "_make_api_call", wraps=self.client._make_api_call) as calls:
                listed = backup.list_versions(self.client, self.bucket, "map1", page_size=1)
            self.assertTrue(calls.call_args_list)
            self.assertTrue(all(c.args[0] == "ListObjectVersions" for c in calls.call_args_list))
            self.assertFalse(listed["truncated"])
            selected = next(v for v in listed["versions"] if v["key"] == key)
            self.assertEqual(set(selected), {"key", "version_id", "last_modified"})
            destination = self.root / "discovered-recovery"
            backup.download_bundle(self.client, self.bucket, "map1", selected["key"], destination,
                                   manifest_version_id=selected["version_id"])
        for source in self.bundle.iterdir():
            self.assertEqual(source.read_bytes(), (destination / source.name).read_bytes())

    def test_version_discovery_bounds_pages_and_resumes_past_delete_markers(self):
        key = self.upload()["completion_keys"][0]
        self.client.delete_object(Bucket=self.bucket, Key=key)
        with patch.object(self.client, "_make_api_call", wraps=self.client._make_api_call) as calls:
            first = backup.list_versions(self.client, self.bucket, "map1", max_pages=1, page_size=1)
        self.assertEqual(calls.call_count, 1)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["versions"], [])  # The first provider entry is the delete marker.
        remaining = backup.list_versions(self.client, self.bucket, "map1", page_size=1,
                                         key_marker=first["next_key_marker"],
                                         version_id_marker=first["next_version_id_marker"])
        self.assertTrue(any(v["key"] == key for v in remaining["versions"]))

    def test_version_discovery_ignores_archives_invalid_keys_and_other_prefixes(self):
        key = self.upload()["completion_keys"][0]
        for extra in ("map1/daily/invalid/COMPLETE.json", "map1/daily/not-a-marker.age", "other/daily/private/COMPLETE.json"):
            self.client.put_object(Bucket=self.bucket, Key=extra, Body=b"fixture")
        listed = backup.list_versions(self.client, self.bucket, "map1")
        self.assertEqual([v["key"] for v in listed["versions"]], [key])
        for arguments in ({"max_pages": 0}, {"max_pages": 101}, {"page_size": 1001},
                          {"group": "other"}, {"key_marker": "other/daily/cursor"}, {"version_id_marker": "orphan"}):
            with self.subTest(arguments=arguments), patch.object(self.client, "get_paginator") as paginator:
                with self.assertRaises(ValueError):
                    backup.list_versions(self.client, self.bucket, "map1", **arguments)
                paginator.assert_not_called()

    def test_cli_forwards_bounded_version_discovery_options(self):
        arguments = ["backup_offsite.py", "list-versions", "--group", "weekly", "--max-pages", "2", "--page-size", "3"]
        with patch.object(sys, "argv", arguments), patch.object(backup, "settings", return_value=(self.client, self.bucket, "map1")):
            with patch.object(backup, "list_versions", return_value={"versions": []}) as listing:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(backup.main(), 0)
                self.assertEqual(listing.call_args.args, (self.client, self.bucket, "map1", "weekly", 2, 3, None, None))

    def test_cli_forwards_explicit_manifest_version(self):
        arguments = ["backup_offsite.py", "download", "completion-key", str(self.root / "cli"),
                     "--manifest-version-id", "retained-marker-version"]
        with patch.object(sys, "argv", arguments), patch.object(backup, "settings", return_value=(self.client, self.bucket, "map1")):
            with patch.object(backup, "download_bundle", return_value={"status": "fixture"}) as download:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(backup.main(), 0)
                self.assertEqual(download.call_args.args[-1], "retained-marker-version")


if __name__ == "__main__":
    unittest.main()
