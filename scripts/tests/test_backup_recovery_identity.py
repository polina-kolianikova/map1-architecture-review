"""Frozen public identity and retained sidecar regressions; local AWS emulator only."""
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import boto3
from moto import mock_aws

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_identity_test_module", SCRIPTS / "backup_offsite.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
TEST_OMISSION_MARKER = str("test-only-" + "omission-marker")
REGION = "TR-CURRENT-FOREST-20261114"
GRID = "stage3-current-forest-20261114"


class FrozenIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".recovery-identity-", dir=SCRIPTS / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.configuration = self.root / "configuration"
        self.configuration.mkdir()
        self.runtime = dict(zip(backup.RUNTIME_IDENTITY_KEYS, ("map1_fixture", REGION, GRID, REGION, GRID)))
        (self.configuration / "runtime.env").write_text(
            "".join(key + "=" + value + "\n" for key, value in self.runtime.items())
            + "POSTGRES_PASSWORD=" + TEST_OMISSION_MARKER + "\n")
        self.target = {"ENDPOINT_URL": "https://s3.project.invalid", "REGION": "us-east-1",
                       "BUCKET": "project-identity-test", "PREFIX": "map1", "PROVIDER": "backblaze-b2"}
        self.storage = {"BACKUP_S3_" + key: value for key, value in self.target.items()}
        self.storage.update(BACKUP_S3_ACCESS_KEY_ID="test", BACKUP_S3_SECRET_ACCESS_KEY=TEST_OMISSION_MARKER,
                            BACKUP_S3_OBJECT_LOCK_MODE="COMPLIANCE")
        (self.configuration / "backup-s3.env").write_text(
            "".join(key + "=" + value + "\n" for key, value in self.storage.items()))
        source = self.configuration / "source.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            member = tarfile.TarInfo("backend/app/main.py")
            member.size = 5
            archive.addfile(member, io.BytesIO(b"pass\n"))
        self.manifest = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "scope_revision": "a" * 64, "schema_revision": "20261117_0047",
            "application_images": {"backend": "sha256:" + "b"*64, "frontend": "sha256:" + "c"*64},
            "files": {"backend/app/main.py": hashlib.sha256(b"pass\n").hexdigest()}}
        self.manifest_path = self.configuration / "source-manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))

    def identity(self):
        return backup.build_recovery_identity(self.configuration)

    def vpn_inputs(self):
        live=self.root/"vpn-live";build=self.root/"vpn-build";build.mkdir(exist_ok=True)
        for name in backup.VPN_SERVER_FILES:
            target=live/name.lstrip("/");target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((TEST_OMISSION_MARKER+" "+name).encode())
        for name in backup.VPN_BUILD_FILES:
            (build/name).write_bytes((TEST_OMISSION_MARKER+" "+name).encode())
        return live,build

    def seal_vpn(self):
        # Simulated age encryption of synthetic test inputs, like job regressions.
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode="w:gz") as archive:
            archive.add(self.configuration/"vpn",arcname=".")
        raw=b"age-encryption.org/v1\n"+output.getvalue()
        (self.root/backup.VPN_ARCHIVE_NAME).write_bytes(raw)
        shutil.rmtree(self.configuration/"vpn")
        return raw

    def test_current_vpn_freeze_and_public_ciphertext_identity_use_frozen_inputs(self):
        live,build=self.vpn_inputs()
        private=backup.freeze_current_vpn(build,self.configuration,live)
        manifest=json.loads((self.configuration/"vpn/manifest.json").read_text())
        self.assertEqual(manifest["files"],private["files"])
        for name in backup.VPN_MEMBERS:
            self.assertEqual((self.configuration/name).stat().st_mode&0o777,0o600)
        raw=self.seal_vpn();first=self.identity()
        component=first["components"]["ogm_vpn"]
        self.assertEqual(set(component),{"sha256","bytes","captured_at","role"})
        self.assertEqual(component["sha256"],hashlib.sha256(raw).hexdigest())
        for name in backup.VPN_SERVER_FILES:(live/name.lstrip("/")).write_bytes(b"later live input")
        for name in backup.VPN_BUILD_FILES:(build/name).write_bytes(b"later build source")
        self.assertEqual(self.identity(),first)
        for value in (TEST_OMISSION_MARKER,"auth.txt","client.ovpn","supervisor.py"):
            self.assertNotIn(value,json.dumps(component))

    def test_current_vpn_missing_symlink_or_oversize_fails_before_staging(self):
        live,build=self.vpn_inputs()
        source=live/backup.VPN_SERVER_FILES[0].lstrip("/")
        raw=source.read_bytes();source.unlink()
        with self.assertRaises(OSError):backup.freeze_current_vpn(build,self.configuration,live)
        source.symlink_to(build/"Dockerfile")
        with self.assertRaises(ValueError):backup.freeze_current_vpn(build,self.configuration,live)
        source.unlink();source.write_bytes(b"x"*200001)
        with self.assertRaises(ValueError):backup.freeze_current_vpn(build,self.configuration,live)
        self.assertFalse((self.configuration/"vpn").exists())

    def test_current_capture_bootstrap_is_stdlib_only_and_logs_no_private_paths(self):
        result=subprocess.run([sys.executable,"-B","-S",str(SCRIPTS/"backup_offsite.py"),
            "freeze-vpn-current"],env=dict(os.environ,PYTHONPATH="",PATH="/usr/bin:/bin"),
            capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn("boto3",result.stderr)
        self.assertNotIn(TEST_OMISSION_MARKER,result.stdout+result.stderr)

    def test_strict_nonsecret_projection_and_raw_source_manifest_digest(self):
        identity = self.identity()
        self.assertEqual(identity["source_manifest_sha256"], hashlib.sha256(self.manifest_path.read_bytes()).hexdigest())
        self.assertEqual(identity["source_sha256"], self.manifest["source_sha256"])
        self.assertEqual(identity["runtime_target"], self.runtime)
        self.assertEqual(identity["storage_target"], self.target)
        encoded = json.dumps(identity)
        self.assertNotIn(TEST_OMISSION_MARKER, encoded)
        for key in ("run_id", "source_run_id", "source_layer_b_run_id", "POSTGRES_PASSWORD",
                    "BACKUP_S3_SECRET_ACCESS_KEY"):
            self.assertNotIn(key, encoded)

    def test_stdlib_only_builder_writes_exclusively_and_does_not_need_age(self):
        destination = self.root / "recovery-identity.json"
        env = dict(os.environ, PYTHONPATH="", PATH="/usr/bin:/bin")
        args = [sys.executable, "-B", "-S", str(SCRIPTS / "backup_offsite.py"),
                "build-identity", str(self.configuration), str(destination)]
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(destination.read_text()), self.identity())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TEST_OMISSION_MARKER, result.stdout + result.stderr)
        second = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(json.loads(destination.read_text()), self.identity())

    def test_missing_manifest_metadata_and_source_mismatch_are_rejected(self):
        for key, value in [("source_sha256", "0"*64), ("scope_revision", None),
                           ("schema_revision", None), ("application_images", {})]:
            with self.subTest(key=key):
                self.manifest_path.write_text(json.dumps(dict(self.manifest, **{key: value})))
                with self.assertRaises(ValueError):
                    self.identity()
        self.manifest_path.write_text(json.dumps(self.manifest))

    def test_effective_override_scope_is_frozen_and_database_override_must_agree(self):
        override = self.configuration / "deploy.override.json"
        override.write_text(json.dumps({"services":{"backend":{"environment":{
            "ACTIVE_REGION_CODE":"TR-OTHER","STAGE4_REGION_CODE":"TR-OTHER"}}}}))
        self.assertEqual(self.identity()["region_code"], "TR-OTHER")
        override.write_text(json.dumps({"services":{"backend":{"environment":{"POSTGRES_DB":"different"}}}}))
        with self.assertRaises(ValueError):
            self.identity()

    def test_ambiguous_scope_interpolation_and_secret_fields_are_rejected(self):
        runtime = self.configuration / "runtime.env"
        original = runtime.read_text()
        runtime.write_text(original + "ACTIVE_REGION_CODE=OTHER\n")
        with self.assertRaises(ValueError):
            self.identity()
        runtime.write_text(original)
        identity = self.identity()
        for change in [lambda x:x.update(password=TEST_OMISSION_MARKER),
                       lambda x:x["runtime_target"].update(POSTGRES_PASSWORD=TEST_OMISSION_MARKER),
                       lambda x:x["storage_target"].update(SECRET_ACCESS_KEY=TEST_OMISSION_MARKER),
                       lambda x:x.update(grid_version="wrong"),
                       lambda x:x["runtime_target"].update(ACTIVE_GRID_VERSION="$"+"{UNRESOLVED}")]:
            with self.subTest(change=change):
                changed = copy.deepcopy(identity); change(changed)
                with self.assertRaises(ValueError):
                    backup.validate_recovery_identity(changed)

    def test_identity_uses_frozen_inputs_not_later_live_settings(self):
        original = self.identity()
        with patch.dict(os.environ, {"ACTIVE_REGION_CODE":"other", "BACKUP_S3_BUCKET":"other"}):
            self.assertEqual(self.identity(), original)


@mock_aws
class RetainedIdentityTest(unittest.TestCase):
    identity = FrozenIdentityTest.identity
    vpn_inputs = FrozenIdentityTest.vpn_inputs
    seal_vpn = FrozenIdentityTest.seal_vpn

    def setUp(self):
        FrozenIdentityTest.setUp(self)
        patcher = patch.dict(os.environ, self.storage, clear=False)
        patcher.start(); self.addCleanup(patcher.stop)
        space = patch.object(backup.os, "statvfs", return_value=SimpleNamespace(
            f_bavail=1024**4, f_frsize=1, f_favail=1_000_000))
        space.start(); self.addCleanup(space.stop)
        self.client = boto3.client("s3", region_name="us-east-1",
                                   aws_access_key_id="test", aws_secret_access_key="test")
        self.client.create_bucket(Bucket=self.target["BUCKET"], ObjectLockEnabledForBucket=True)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "native.dump.age").write_bytes(b"age-encryption.org/v1\nsynthetic database")
        (self.bundle / "configuration.tar.age").write_bytes(b"age-encryption.org/v1\nsynthetic encrypted configuration")
        (self.bundle / backup.IDENTITY_NAME).write_text(json.dumps(self.identity(), sort_keys=True) + "\n")

    def upload(self):
        return backup.upload_bundle(self.client, self.target["BUCKET"], "map1", self.bundle)

    def inspect(self, receipt):
        selected = receipt["completion_versions"][0]
        return backup.inspect_recovery_identity(self.client, self.target["BUCKET"], "map1",
                                                selected["key"], selected["version_id"], self.target)

    def add_vpn(self):
        live,build=self.vpn_inputs()
        backup.freeze_current_vpn(build,self.configuration,live)
        raw=self.seal_vpn()
        (self.bundle/backup.VPN_ARCHIVE_NAME).write_bytes(raw)
        (self.bundle/backup.IDENTITY_NAME).write_text(json.dumps(self.identity(),sort_keys=True)+"\n")

    def test_four_file_bundle_exposes_only_vpn_ciphertext_identity(self):
        self.add_vpn();receipt=self.upload()
        with patch.object(self.client,"get_object",wraps=self.client.get_object) as get:
            proof=self.inspect(receipt)
        self.assertEqual(len(proof["remote_manifest"]["files"]),4)
        self.assertEqual(proof["verified_components"],self.identity()["components"])
        keys=[x.kwargs["Key"] for x in get.call_args_list]
        self.assertEqual(len(keys),3)
        self.assertEqual([x.rsplit("/",1)[-1] for x in keys if x.endswith(".age")],[backup.VPN_ARCHIVE_NAME])
        component=proof["recovery_identity"]["components"]["ogm_vpn"]
        self.assertEqual(set(component),{"sha256","bytes","captured_at","role"})
        self.assertNotIn("auth.txt",json.dumps(proof["recovery_identity"]))
        selected=receipt["completion_versions"][0];target=self.root/"vpn-download"
        result=backup.download_bundle(self.client,self.target["BUCKET"],"map1",selected["key"],
                                     target,selected["version_id"])
        self.assertEqual(result["archives"],4)
        raw=(target/backup.VPN_ARCHIVE_NAME).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(raw.split(b"\n",1)[1]),mode="r:gz") as archive:
            manifest=json.load(archive.extractfile("./manifest.json"))
            self.assertEqual(set(manifest["files"]),set(backup.VPN_MEMBERS))
            self.assertIn("./restore-instructions.txt",archive.getnames())
        self.assertNotIn(TEST_OMISSION_MARKER,(target/backup.IDENTITY_NAME).read_text())

    def test_current_vpn_sidecar_remote_corruption_is_rejected(self):
        self.add_vpn();receipt=self.upload()
        original=self.client.get_object
        def corrupt(**kwargs):
            result=original(**kwargs)
            if kwargs["Key"].endswith(backup.IDENTITY_NAME):
                raw=result["Body"].read();result["Body"].close()
                result["Body"]=io.BytesIO(raw[:-1]+b"!")
            return result
        with patch.object(self.client,"get_object",side_effect=corrupt),self.assertRaises(ValueError):
            self.inspect(receipt)

    def test_sidecar_hashed_read_back_retained_and_downloadable(self):
        receipt = self.upload()
        proof = self.inspect(receipt)
        self.assertEqual(proof["recovery_identity"], self.identity())
        files = proof["remote_manifest"]["files"]
        self.assertEqual(len(files), 3)
        sidecar = next(x for x in files if x["name"] == backup.IDENTITY_NAME)
        self.assertEqual(proof["recovery_identity_sha256"], sidecar["sha256"])
        self.assertTrue(all(x.get("version_id") for x in files))
        self.assertEqual(proof["remote_manifest"]["object_lock"]["mode"], "COMPLIANCE")
        selected = receipt["completion_versions"][0]
        destination = self.root / "downloaded"
        result = backup.download_bundle(self.client, self.target["BUCKET"], "map1", selected["key"],
                                         destination, selected["version_id"])
        self.assertEqual(result["archives"], 3)
        self.assertEqual((destination / backup.IDENTITY_NAME).read_bytes(),
                         (self.bundle / backup.IDENTITY_NAME).read_bytes())


    def test_inspection_never_gets_dump_or_encrypted_configuration(self):
        receipt = self.upload()
        with patch.object(self.client, "get_object", wraps=self.client.get_object) as get:
            self.inspect(receipt)
        keys = [call.kwargs["Key"] for call in get.call_args_list]
        self.assertEqual(len(keys), 2)
        self.assertFalse(any(key.endswith(".age") for key in keys))

    def test_legacy_two_file_read_restore_remains_compatible_but_not_final_identity(self):
        (self.bundle / backup.IDENTITY_NAME).unlink()
        receipt = self.upload()
        selected = receipt["completion_versions"][0]
        manifest = backup.read_manifest(self.client, self.target["BUCKET"], selected["key"], selected["version_id"])
        self.assertEqual(len(manifest["files"]), 2)
        destination = self.root / "legacy-download"
        self.assertEqual(backup.download_bundle(self.client, self.target["BUCKET"], "map1",
                         selected["key"], destination, selected["version_id"])["archives"], 2)
        with self.assertRaisesRegex(ValueError, "Legacy bundle"):
            self.inspect(receipt)

    def test_wrong_storage_target_rejected_before_upload(self):
        changed = self.identity()
        changed["storage_target"]["BUCKET"] = "another-project"
        (self.bundle / backup.IDENTITY_NAME).write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.upload()
        self.assertNotIn("Contents", self.client.list_objects_v2(Bucket=self.target["BUCKET"]))

    def test_remote_sidecar_same_size_corruption_is_rejected(self):
        receipt = self.upload()
        original = self.client.get_object
        def corrupt(**kwargs):
            result = original(**kwargs)
            if kwargs["Key"].endswith(backup.IDENTITY_NAME):
                raw = result["Body"].read(); result["Body"].close()
                result["Body"] = io.BytesIO(b"!" * len(raw))
            return result
        with patch.object(self.client, "get_object", side_effect=corrupt),self.assertRaises(ValueError):
            self.inspect(receipt)

    def test_only_known_public_sidecar_name_is_allowed(self):
        now = datetime.now(timezone.utc).isoformat()
        manifest = {"format":1,"snapshot_at":now,"created_at":now,"files":[
            {"name":"native.dump.age","size":1,"sha256":"a"*64},
            {"name":"configuration.tar.age","size":1,"sha256":"b"*64},
            {"name":"private-runtime.json","size":1,"sha256":"c"*64}]}
        with self.assertRaises(ValueError):
            backup.validate_manifest(manifest)
        manifest["files"][-1]["name"] = backup.IDENTITY_NAME
        backup.validate_manifest(manifest)
        manifest["files"][-1]["size"] = backup.IDENTITY_MAX_BYTES + 1
        with self.assertRaises(ValueError):
            backup.validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
