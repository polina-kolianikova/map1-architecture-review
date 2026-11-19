#!/usr/bin/env python3
"""Transfer encrypted recovery bundles to independent S3-compatible storage.

Generic S3 publishes COMPLETE with a conditional write. Explicit
BACKUP_S3_PROVIDER=backblaze-b2 uses COMPLIANCE-retained versions instead:
the marker key can receive later versions. Use completion_versions receipts or
read-only list-versions discovery, then --manifest-version-id to recover a marker
version even if its current name is hidden and the original VPS is unavailable.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

# Public metadata only. Host creation must work without importing the S3 SDK.
IDENTITY_NAME = "recovery-identity.json"
IDENTITY_MAX_BYTES = 64 * 1024
VPN_ARCHIVE_NAME = "ogm-vpn-recovery.tar.age"
VPN_MAX_BYTES = 1024 * 1024
VPN_CIPHERTEXT_MAX_BYTES = 2 * 1024 * 1024
VPN_ROLE = "ogm-vpn-recovery"
VPN_SERVER_FILES = (
    "/etc/map1-ogm-vpn/client.ovpn", "/etc/map1-ogm-vpn/auth.txt",
    "/etc/systemd/system/map1-ogm-openvpn-proxy.service",
    "/opt/map1-ogm-vpn/run.sh", "/opt/map1-ogm-vpn/stop.py", "/opt/map1-ogm-vpn/health.py",
)
VPN_BUILD_FILES = ("Dockerfile", ".dockerignore", "supervisor.py")
VPN_MEMBERS = tuple("vpn/server" + path for path in VPN_SERVER_FILES) + tuple("vpn/source/" + name for name in VPN_BUILD_FILES)
RUNTIME_IDENTITY_KEYS = ("POSTGRES_DB", "ACTIVE_REGION_CODE", "ACTIVE_GRID_VERSION",
                         "STAGE4_REGION_CODE", "STAGE4_GRID_VERSION")
STORAGE_IDENTITY_KEYS = ("ENDPOINT_URL", "REGION", "BUCKET", "PREFIX", "PROVIDER")


def bounded_file(path, limit):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limit:
        raise ValueError("Recovery identity input must be a bounded regular file")
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Recovery identity input exceeds its size cap")
    return raw


def identity_environment(path, keys):
    values = {}
    for line in bounded_file(path, 2 * 1024**2).decode().splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in keys:
            continue
        if key in values:
            raise ValueError("Ambiguous recovery identity environment")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values


def frozen_vpn_identity(configuration):
    """PRIVATE member hashes, kept inside the encrypted VPN archive only."""
    hashes, total = {}, 0
    for name in VPN_MEMBERS:
        raw = bounded_file(configuration / name, 200000)
        total += len(raw)
        if not raw or total > VPN_MAX_BYTES:
            raise ValueError("Frozen VPN component is empty or exceeds its size cap")
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return {"files": hashes}


def freeze_current_vpn(build_directory, configuration, server_root=Path("/")):
    """Read exactly six current project files + three rebuild files; no scan."""
    destination = configuration / "vpn"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Frozen VPN destination must not already exist")
    sources = [(name, server_root / source.lstrip("/"))
               for name, source in zip(VPN_MEMBERS[:6], VPN_SERVER_FILES)]
    sources += [("vpn/source/" + name, build_directory / name) for name in VPN_BUILD_FILES]
    frozen, total = {}, 0
    for name, source in sources:
        before = source.stat()
        raw = bounded_file(source, 200000)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("VPN input changed while freezing")
        total += len(raw)
        if not raw or total > VPN_MAX_BYTES:
            raise ValueError("VPN component is empty or exceeds its bounded size")
        frozen[name] = raw
    destination.mkdir(mode=0o700)
    for name, raw in frozen.items():
        target = configuration / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write(raw)
    private = frozen_vpn_identity(configuration)
    # Match snapshot-at.txt's native whole-second precision.
    captured = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest = {"format": 1, "role": VPN_ROLE, "captured_at": captured, "files": private["files"]}
    with (destination / "manifest.json").open("x") as output:
        json.dump(manifest, output, sort_keys=True)
    with (destination / "restore-instructions.txt").open("x") as output:
        output.write("Decrypt this archive on the authorized recovery host. Restore server/ to the matching absolute project paths with private permissions. The source/ directory contains Dockerfile, .dockerignore and supervisor.py for rebuilding the VPN supervisor. Restore the systemd unit and validate the tunnel before enabling future OGM acquisition. Keep the offline age identity off the application server.\n")
    with (configuration.parent / "vpn-captured-at.txt").open("x") as output:
        output.write(captured + "\n")
    return private


def encrypted_vpn_identity(configuration):
    """Public metadata: ciphertext hash/size, capture time and role ONLY."""
    raw = bounded_file(configuration.parent / VPN_ARCHIVE_NAME, VPN_CIPHERTEXT_MAX_BYTES)
    if not raw.startswith(b"age-encryption.org/v1\n"):
        raise ValueError("VPN recovery archive must be encrypted")
    captured = bounded_file(configuration.parent / "vpn-captured-at.txt", 100).decode().strip()
    at = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    if at.tzinfo is None or at > datetime.now(timezone.utc):
        raise ValueError("Invalid VPN capture time")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "captured_at": captured, "role": VPN_ROLE}


def validate_recovery_identity(value):
    fields = {"format", "source_sha256", "source_manifest_sha256", "application_images",
              "schema_revision", "region_code", "grid_version", "scope_revision",
              "runtime_target", "storage_target"}
    if not isinstance(value, dict) or set(value) not in (fields, fields | {"components"}) or type(value["format"]) is not int or value["format"] != 1:
        raise ValueError("Unsupported recovery identity")
    for key in ("source_sha256", "source_manifest_sha256", "scope_revision"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            raise ValueError("Invalid recovery identity digest")
    images = value["application_images"]
    if (not isinstance(images, dict) or set(images) != {"backend", "frontend"}
            or any(not isinstance(x, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", x) for x in images.values())):
        raise ValueError("Invalid recovery application images")
    if not isinstance(value["schema_revision"], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", value["schema_revision"]):
        raise ValueError("Invalid recovery schema revision")
    runtime = value["runtime_target"]
    if not isinstance(runtime, dict) or set(runtime) != set(RUNTIME_IDENTITY_KEYS):
        raise ValueError("Invalid recovery runtime target")
    if not isinstance(runtime["POSTGRES_DB"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime["POSTGRES_DB"]):
        raise ValueError("Invalid recovery database target")
    for key in RUNTIME_IDENTITY_KEYS[1:]:
        if not isinstance(runtime[key], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", runtime[key]):
            raise ValueError("Invalid recovery scope target")
    if (value["region_code"] != runtime["ACTIVE_REGION_CODE"] or
            value["grid_version"] != runtime["ACTIVE_GRID_VERSION"] or
            runtime["STAGE4_REGION_CODE"] != value["region_code"] or
            runtime["STAGE4_GRID_VERSION"] != value["grid_version"]):
        raise ValueError("Recovery scope targets disagree")
    target = value["storage_target"]
    if not isinstance(target, dict) or set(target) != set(STORAGE_IDENTITY_KEYS):
        raise ValueError("Invalid recovery storage target")
    if any(not isinstance(x, str) or not x or len(x) > 1024 or any(ord(c) < 32 for c in x) for x in target.values()):
        raise ValueError("Invalid recovery storage target")
    endpoint = urlsplit(target["ENDPOINT_URL"])
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or target["PROVIDER"] not in ("s3", "backblaze-b2")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,150}", target["PREFIX"])
            or ".." in target["PREFIX"].split("/")):
        raise ValueError("Unsafe recovery storage target")
    if "components" in value:
        components = value["components"]
        if not isinstance(components, dict) or set(components) != {"ogm_vpn"}:
            raise ValueError("Unsupported recovery component")
        component = components["ogm_vpn"]
        if (not isinstance(component, dict) or set(component) != {"sha256", "bytes", "captured_at", "role"}
                or component["role"] != VPN_ROLE or not isinstance(component["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", component["sha256"])
                or type(component["bytes"]) is not int or not 22 < component["bytes"] <= VPN_CIPHERTEXT_MAX_BYTES):
            raise ValueError("Invalid encrypted VPN component identity")
        at = datetime.fromisoformat(component["captured_at"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("VPN capture time requires a time zone")
    return value


def build_recovery_identity(configuration):
    """Use the SAME frozen inputs as backup-offsite.sh for this dump."""
    manifest_raw = bounded_file(configuration / "source-manifest.json", 2 * 1024**2)
    manifest = json.loads(manifest_raw)
    source_sha = hashlib.sha256(bounded_file(configuration / "source.tar.gz", 16 * 1024**2)).hexdigest()
    if manifest.get("source_sha256") != source_sha:
        raise ValueError("Frozen source does not match its source manifest")
    runtime = identity_environment(configuration / "runtime.env", RUNTIME_IDENTITY_KEYS)
    override = configuration / "deploy.override.json"
    if override.exists():
        services = json.loads(bounded_file(override, 2 * 1024**2)).get("services", {})
        env = services.get("backend", {}).get("environment", {})
        if isinstance(env, list):
            selected = {}
            for item in env:
                key, sep, value = item.partition("=")
                if key in RUNTIME_IDENTITY_KEYS:
                    if not sep or key in selected:
                        raise ValueError("Ambiguous effective recovery target")
                    selected[key] = value
            env = selected
        if not isinstance(env, dict):
            raise ValueError("Unsupported recovery deployment environment")
        for key in RUNTIME_IDENTITY_KEYS:
            if key in env:
                if key == "POSTGRES_DB" and env[key] != runtime.get(key):
                    raise ValueError("Effective application and dump database targets disagree")
                runtime[key] = env[key]
        db_env = services.get("db", {}).get("environment", {})
        if db_env and (not isinstance(db_env, dict) or
                       db_env.get("POSTGRES_DB", runtime.get("POSTGRES_DB")) != runtime.get("POSTGRES_DB")):
            raise ValueError("Effective dump database target is ambiguous")
    storage = identity_environment(configuration / "backup-s3.env",
                                   tuple("BACKUP_S3_" + key for key in STORAGE_IDENTITY_KEYS))
    target = {key: storage.get("BACKUP_S3_" + key, "") for key in STORAGE_IDENTITY_KEYS}
    target["PREFIX"] = storage.get("BACKUP_S3_PREFIX", "map1").strip("/")
    target["PROVIDER"] = storage.get("BACKUP_S3_PROVIDER") or "s3"
    identity = {
        "format": 1, "source_sha256": source_sha,
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "application_images": manifest.get("application_images"),
        "schema_revision": manifest.get("schema_revision"),
        "scope_revision": manifest.get("scope_revision"),
        "region_code": runtime.get("ACTIVE_REGION_CODE"),
        "grid_version": runtime.get("ACTIVE_GRID_VERSION"),
        "runtime_target": runtime, "storage_target": target,
    }
    if (configuration / "vpn").exists():
        raise ValueError("VPN plaintext staging must be encrypted before public identity creation")
    if (configuration.parent / VPN_ARCHIVE_NAME).exists():
        identity["components"] = {"ogm_vpn": encrypted_vpn_identity(configuration)}
    return validate_recovery_identity(identity)


def public_storage_target(bucket, prefix):
    return {"ENDPOINT_URL": os.environ.get("BACKUP_S3_ENDPOINT_URL", ""),
            "REGION": os.environ.get("BACKUP_S3_REGION", ""),
            "BUCKET": bucket, "PREFIX": prefix,
            "PROVIDER": os.environ.get("BACKUP_S3_PROVIDER") or "s3"}


if __name__ == "__main__" and sys.argv[1:2] in (["build-identity"], ["freeze-vpn-current"]):
    # No SDK, Docker, network, age invocation or recovery private key needed.
    os.umask(0o077)
    try:
        if sys.argv[1] == "freeze-vpn-current":
            if len(sys.argv) != 4:
                raise ValueError("Expected current build directory and frozen configuration")
            identity = freeze_current_vpn(Path(sys.argv[2]), Path(sys.argv[3]))
            print(json.dumps({"status": "current_vpn_frozen", "files": len(identity["files"])}))
        else:
            if len(sys.argv) != 4:
                raise ValueError("Expected frozen directory and identity destination")
            raw = (json.dumps(build_recovery_identity(Path(sys.argv[2])), sort_keys=True) + "\n").encode()
            if len(raw) > IDENTITY_MAX_BYTES:
                raise ValueError("Recovery identity exceeds its size cap")
            with Path(sys.argv[3]).open("xb") as output:
                output.write(raw)
            print(json.dumps({"status": "recovery_identity_created", "sha256": hashlib.sha256(raw).hexdigest()}))
    except Exception:
        print('{"status":"failed","operation":"build-identity","reason":"invalid_frozen_identity"}', file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.handlers import add_expect_header
from botocore.exceptions import ClientError


TRANSFER = TransferConfig(multipart_threshold=64 * 1024**2, multipart_chunksize=64 * 1024**2,
                          max_concurrency=2)
NAME = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.age|recovery-identity\.json)\Z")
RETENTION_DAYS = {"daily": 7, "weekly": 28}
RESTORE_MAX_BYTES = 128 * 1024**3
RESTORE_MIN_FREE_BYTES = 40 * 1024**3
RESTORE_MIN_FREE_INODES = 100_000


def safe_failure(error, operation):
    """Classify failures without logging provider messages, URLs or identifiers.

    Managed uploads wrap ClientError in S3UploadFailedError. Follow the exception
    chain, not its string representation, to retain actionable provider status.
    Every provider-derived string is allowlisted; unknown values stay redacted.
    """
    types = {"ClientError", "S3UploadFailedError", "ValueError", "OSError",
             "TimeoutError", "EndpointConnectionError", "ConnectTimeoutError",
             "ReadTimeoutError", "NoCredentialsError", "PartialCredentialsError"}
    result = {"status": "failed", "operation": operation,
              "error_type": type(error).__name__ if type(error).__name__ in types else "other"}
    codes = {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken",
             "BadDigest", "InvalidRequest", "InvalidArgument", "NotImplemented",
             "ServiceUnavailable", "InternalError", "RequestTimeout", "SlowDown",
             "EntityTooLarge", "InvalidPart", "NoSuchBucket", "NoSuchKey",
             "NoSuchVersion", "NoSuchUpload", "PreconditionFailed", "403", "401", "404"}
    operations = {"PutObject", "CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload",
                  "AbortMultipartUpload", "HeadObject", "GetObject", "ListObjectsV2",
                  "ListObjectVersions"}
    reasons = {
        "No completed external backup exists": "no_completed_backup",
        "Latest completed external backup is stale or has an invalid timestamp": "stale_or_invalid_snapshot",
        "HEAD did not confirm the required COMPLIANCE retention": "retention_unconfirmed",
    }
    seen = set()
    current = error
    for _ in range(10):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ClientError):
            response = current.response
            code = response.get("Error", {}).get("Code")
            result["error_code"] = code if isinstance(code, str) and code in codes else "other"
            name = current.operation_name
            result["provider_operation"] = name if isinstance(name, str) and name in operations else "other"
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if type(status) is int and 100 <= status <= 599:
                result["http_status"] = status
            message = response.get("Error", {}).get("Message", "")
            if isinstance(message, str) and "storage cap exceeded" in message.lower():
                result["reason"] = "storage_cap_exceeded"
            break
        if isinstance(current, ValueError) and str(current) in reasons:
            result["reason"] = reasons[str(current)]
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return result


def storage_provider():
    provider = os.environ.get("BACKUP_S3_PROVIDER") or "s3"
    if provider not in ("s3", "backblaze-b2"):
        raise ValueError("Backup provider must be s3 or explicit backblaze-b2")
    return provider


def object_lock_enabled():
    mode = os.environ.get("BACKUP_S3_OBJECT_LOCK_MODE", "")
    if mode not in ("", "COMPLIANCE"):
        raise ValueError("Optional S3 Object Lock mode must be COMPLIANCE")
    if storage_provider() == "backblaze-b2" and mode != "COMPLIANCE":
        raise ValueError("The Backblaze B2 adapter requires COMPLIANCE Object Lock")
    return mode == "COMPLIANCE"


def content_md5(body):
    """Transport checksum only; SHA-256 remains the backup integrity check."""
    checksum = hashlib.md5(usedforsecurity=False)
    if isinstance(body, (bytes, bytearray)):
        checksum.update(body)
    elif body is not None:
        if not all(hasattr(body, method) for method in ("read", "seek", "tell")):
            raise ValueError("B2 uploads require a seekable body for Content-MD5")
        position = body.tell()
        try:
            for block in iter(lambda: body.read(1024 * 1024), b""):
                checksum.update(block)
        finally:
            body.seek(position)
    return base64.b64encode(checksum.digest()).decode("ascii")


def b2_content_md5(params, **kwargs):
    # Always calculate the header here: botocore's conditional helper can skip it
    # when the SDK checksum context is populated, leaving B2 Object Lock PUTs invalid.
    params["headers"]["Content-MD5"] = content_md5(params.get("body"))


def version_arguments(version_id):
    if (not isinstance(version_id, str) or not 1 <= len(version_id) <= 1024
            or version_id == "null" or any(ord(char) < 32 or ord(char) == 127 for char in version_id)):
        raise ValueError("A non-null object VersionId is required")
    return {"VersionId": version_id}


def verify_version(result, version_id):
    if version_id is not None and result.get("VersionId") != version_id:
        raise ValueError("Storage returned a different object version")


def retention_deadline(created_at, group):
    return created_at.astimezone(timezone.utc).replace(microsecond=0) + timedelta(days=RETENTION_DAYS[group])


def lock_arguments(lock):
    if lock is None:
        return {}
    return {"ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": datetime.fromisoformat(lock["retain_until"])}


def verify_lock(result, lock):
    if lock is None:
        return
    retained_until = result.get("ObjectLockRetainUntilDate")
    if (result.get("ObjectLockMode") != "COMPLIANCE" or not isinstance(retained_until, datetime)
            or retained_until.tzinfo is None
            or retained_until < datetime.fromisoformat(lock["retain_until"])):
        raise ValueError("HEAD did not confirm the required COMPLIANCE retention")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def settings(*, read_timeout=120, retries=4):
    object_lock_enabled()
    values = {key: os.environ.get("BACKUP_S3_" + key, "") for key in
              ("ENDPOINT_URL", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")}
    if not all(values.values()):
        raise ValueError("Independent S3 endpoint, region, bucket and dedicated credentials are required")
    endpoint = urlsplit(values["ENDPOINT_URL"])
    local_test = os.environ.get("BACKUP_S3_LOCAL_TEST") == "1"
    local_http = local_test and endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "localhost", "::1")
    if (endpoint.scheme != "https" and not local_http) or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError("S3 requires HTTPS; plain HTTP is allowed only for an explicit loopback test")
    if endpoint.query or endpoint.fragment:
        raise ValueError("S3 endpoint must not contain a query or fragment")
    prefix = os.environ.get("BACKUP_S3_PREFIX", "map1").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,150}", prefix) or ".." in prefix.split("/"):
        raise ValueError("Invalid S3 backup prefix")
    provider = storage_provider()
    config_options = {"request_checksum_calculation": "when_required"} if provider == "backblaze-b2" else {}
    client = boto3.client("s3", endpoint_url=values["ENDPOINT_URL"], region_name=values["REGION"],
                          aws_access_key_id=values["ACCESS_KEY_ID"],
                          aws_secret_access_key=values["SECRET_ACCESS_KEY"],
                          config=Config(connect_timeout=15, read_timeout=read_timeout,
                                        retries={"mode": "standard", "max_attempts": retries},
                                        s3={"addressing_style": "path"}, **config_options))
    if provider == "backblaze-b2":
        # Dedicated-client transport compatibility; never modify global SDK handlers.
        client.meta.events.unregister("before-call.s3", add_expect_header)
        client.meta.events.register("before-call.s3.PutObject", b2_content_md5)
    return client, values["BUCKET"], prefix


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise ValueError("Unsupported backup manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not 2 <= len(files) <= 10:
        raise ValueError("Recovery bundle must contain database and configuration archives")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not NAME.fullmatch(item["name"]):
            raise ValueError("Unsafe archive name in manifest")
        if item["name"] in seen or type(item.get("size")) is not int or item["size"] < 1:
            raise ValueError("Invalid or duplicate backup archive")
        if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
            raise ValueError("Missing backup checksum")
        if "version_id" in item:
            version_arguments(item["version_id"])
        if item["name"] == IDENTITY_NAME and item["size"] > IDENTITY_MAX_BYTES:
            raise ValueError("Recovery identity exceeds its size cap")
        if item["name"] == VPN_ARCHIVE_NAME and item["size"] > VPN_CIPHERTEXT_MAX_BYTES:
            raise ValueError("Encrypted VPN component exceeds its size cap")
        seen.add(item["name"])
    if sum(name.endswith(".dump.age") for name in seen) != 1 or "configuration.tar.age" not in seen:
        raise ValueError("Recovery bundle needs one database dump and its configuration archive")
    for field in ("created_at", "snapshot_at"):
        timestamp = datetime.fromisoformat(manifest[field])
        if timestamp.tzinfo is None:
            raise ValueError("Backup timestamps must have time zones")
    if "object_lock" in manifest:
        lock = manifest["object_lock"]
        if not isinstance(lock, dict) or lock.get("mode") != "COMPLIANCE":
            raise ValueError("Invalid backup retention mode")
        retained_until = datetime.fromisoformat(lock["retain_until"])
        if retained_until.tzinfo is None or any("version_id" not in item for item in files):
            raise ValueError("Retained backups require time zones and archive VersionIds")
    return manifest


def read_manifest(client, bucket, key, version_id=None):
    arguments = version_arguments(version_id) if version_id is not None else {}
    result = client.get_object(Bucket=bucket, Key=key, **arguments)
    stream = result["Body"]
    try:
        verify_version(result, version_id)
        if result["ContentLength"] > 1024 * 1024:
            raise ValueError("Backup manifest exceeds size limit")
        data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Backup manifest exceeds size limit")
        manifest = validate_manifest(json.loads(data))
    finally:
        stream.close()
    lock = manifest.get("object_lock")
    if object_lock_enabled() and lock is None:
        raise ValueError("Completion marker does not declare COMPLIANCE retention")
    if lock is not None:
        group = key.split("/")[-3]
        if (group not in RETENTION_DAYS or datetime.fromisoformat(lock["retain_until"])
                < retention_deadline(datetime.fromisoformat(manifest["created_at"]), group)):
            raise ValueError("Completion marker declares insufficient retention")
        # Pin the HEAD to the version just read, even when the caller selected the current marker.
        selected_version = result.get("VersionId")
        arguments = version_arguments(selected_version)
        head = client.head_object(Bucket=bucket, Key=key, **arguments)
        verify_version(head, selected_version)
        verify_lock(head, lock)
    return manifest


def verify_remote(client, bucket, base, manifest, verify_content=False):
    lock = manifest.get("object_lock")
    if object_lock_enabled() and lock is None:
        raise ValueError("Recovery archives do not declare COMPLIANCE retention")
    for item in manifest["files"]:
        version_id = item.get("version_id")
        arguments = version_arguments(version_id) if version_id is not None else {}
        if lock is not None and version_id is None:
            raise ValueError("Retained recovery archive has no VersionId")
        result = client.head_object(Bucket=bucket, Key=base + "/" + item["name"], **arguments)
        verify_version(result, version_id)
        verify_lock(result, lock)
        if result["ContentLength"] != item["size"] or result.get("Metadata", {}).get("sha256") != item["sha256"]:
            raise ValueError("Remote archive size or checksum metadata does not match")
        if verify_content:
            stored = client.get_object(Bucket=bucket, Key=base + "/" + item["name"], **arguments)
            stream = stored["Body"]
            value, size = hashlib.sha256(), 0
            try:
                verify_version(stored, version_id)
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
                    size += len(block)
            finally:
                stream.close()
            if size != item["size"] or value.hexdigest() != item["sha256"]:
                raise ValueError("Stored archive content failed integrity verification")


def upload_bundle(client, bucket, prefix, directory, now=None):
    locked = object_lock_enabled()
    provider = storage_provider()
    now = now or datetime.now(timezone.utc)
    paths = sorted(directory.glob("*.age"))
    identity = directory / IDENTITY_NAME
    if identity.exists() or identity.is_symlink():
        frozen = validate_recovery_identity(json.loads(bounded_file(identity, IDENTITY_MAX_BYTES)))
        if frozen["storage_target"] != public_storage_target(bucket, prefix):
            raise ValueError("Frozen recovery storage target differs from transfer target")
        paths.append(identity)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Recovery archives must be regular files")
    for path in paths:
        if path.name == IDENTITY_NAME:
            continue
        with path.open("rb") as source:
            if source.read(len(b"age-encryption.org/v1\n")) != b"age-encryption.org/v1\n":
                raise ValueError("Recovery archives must be age-encrypted, not merely renamed")
    timestamp_file = directory / "snapshot-at.txt"
    if timestamp_file.exists():
        if timestamp_file.is_symlink() or timestamp_file.stat().st_size > 100:
            raise ValueError("Unsafe snapshot timestamp file")
        snapshot_at = datetime.fromisoformat(timestamp_file.read_text().strip())
        if snapshot_at.tzinfo is None:
            raise ValueError("Snapshot timestamp must have a time zone")
    else:
        dumps = [path for path in paths if path.name.endswith(".dump.age")]
        if len(dumps) != 1:
            raise ValueError("A database archive is required")
        snapshot_at = datetime.fromtimestamp(dumps[0].stat().st_mtime, timezone.utc)
    source_manifest = validate_manifest({"format": 1, "created_at": now.isoformat(), "snapshot_at": snapshot_at.isoformat(), "files": [
        {"name": path.name, "size": path.stat().st_size, "sha256": digest(path)} for path in paths]})
    bundle_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    groups = ["daily", "weekly"] if now.weekday() == 0 else ["daily"]
    completed, completed_versions = [], []
    for group in groups:
        base = f"{prefix}/{group}/{bundle_id}"
        # Daily and weekly are independent copies with independent retention deadlines and versions.
        manifest = dict(source_manifest, files=[dict(item) for item in source_manifest["files"]])
        lock = {"mode": "COMPLIANCE", "retain_until": retention_deadline(now, group).isoformat()} if locked else None
        if lock is not None:
            manifest["object_lock"] = lock
        if provider == "backblaze-b2":
            manifest["completion_protocol"] = "immutable-version"
        for item in manifest["files"]:
            path = directory / item["name"]
            client.upload_file(str(path), bucket, base + "/" + item["name"],
                               ExtraArgs={"Metadata": {"sha256": item["sha256"]}, **lock_arguments(lock)},
                               Config=TRANSFER)
            if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
                raise ValueError("Local archive changed during transfer")
            # The transfer manager does not return a VersionId. Pin the observed version,
            # then verify its full content and retention before publishing the marker.
            head = client.head_object(Bucket=bucket, Key=base + "/" + item["name"])
            version_id = head.get("VersionId")
            if locked or version_id not in (None, "null"):
                version_arguments(version_id)
                item["version_id"] = version_id
            verify_lock(head, lock)
        validate_manifest(manifest)
        verify_remote(client, bucket, base, manifest, verify_content=True)
        key = base + "/COMPLETE.json"
        body = json.dumps(manifest, sort_keys=True).encode()
        marker_arguments = lock_arguments(lock)
        if provider == "backblaze-b2":
            marker_arguments["ContentMD5"] = content_md5(body)
        else:
            marker_arguments["IfNoneMatch"] = "*"
        # B2 does not support the generic conditional marker contract. This is an
        # explicit version-retention protocol, never a retry/fallback after a 501.
        written = client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                                    **marker_arguments)
        marker_version = written.get("VersionId")
        if locked or marker_version not in (None, "null"):
            version_arguments(marker_version)
            completed_versions.append({"key": key, "version_id": marker_version})
        else:
            marker_version = None
        if read_manifest(client, bucket, key, marker_version) != manifest:
            raise ValueError("Remote completion marker could not be verified")
        completed.append(key)
    result = {"status": "uploaded", "remote_content_verified": True, "completion_keys": completed}
    if completed_versions:
        result["completion_versions"] = completed_versions
    if locked:
        result["object_lock_mode"] = "COMPLIANCE"
    if provider == "backblaze-b2":
        result.update(provider=provider, completion_protocol="immutable-version")
    return result


def validate_key(prefix, key):
    expression = re.escape(prefix) + r"/(daily|weekly)/[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}/COMPLETE\.json"
    if not re.fullmatch(expression, key):
        raise ValueError("Completion key is outside the configured backup prefix")


def list_versions(client, bucket, prefix, group="daily", max_pages=10, page_size=1000,
                  key_marker=None, version_id_marker=None):
    """Discover candidate marker versions without reading object bodies or requiring receipts.

    Listing does not prove a candidate's integrity/retention; the version-pinned
    download performs those checks. Bound provider pages (including delete markers),
    and return provider cursors so a new host can continue a larger recovery search.
    """
    if (group not in RETENTION_DAYS or type(max_pages) is not int or not 1 <= max_pages <= 100
            or type(page_size) is not int or not 1 <= page_size <= 1000):
        raise ValueError("Version discovery requires daily/weekly, 1-100 pages and 1-1000 entries per page")
    listing_prefix = f"{prefix}/{group}/"

    def validate_cursor(key, version):
        for value in (key, version):
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 1024
                                      or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError("Invalid version discovery cursor")
        if (key is not None and not key.startswith(listing_prefix)) or (version is not None and key is None):
            raise ValueError("Version discovery cursor is outside the selected prefix")

    validate_cursor(key_marker, version_id_marker)
    arguments = {"Bucket": bucket, "Prefix": listing_prefix, "PaginationConfig": {"PageSize": page_size}}
    if key_marker is not None:
        arguments["KeyMarker"] = key_marker
    if version_id_marker is not None:
        arguments["VersionIdMarker"] = version_id_marker
    result = {"status": "listed_versions", "versions": [], "truncated": False}
    seen = set()
    for page_number, page in enumerate(client.get_paginator("list_object_versions").paginate(**arguments), 1):
        # DeleteMarkers hide current names but are not recoverable completion objects.
        for item in page.get("Versions", []):
            key, version_id, modified = item.get("Key"), item.get("VersionId"), item.get("LastModified")
            if not isinstance(key, str) or not key.startswith(listing_prefix):
                continue
            try:
                validate_key(prefix, key)
                version_arguments(version_id)
            except ValueError:
                continue
            if not isinstance(modified, datetime) or modified.tzinfo is None or (key, version_id) in seen:
                continue
            seen.add((key, version_id))
            result["versions"].append({"key": key, "version_id": version_id,
                                       "last_modified": modified.astimezone(timezone.utc).isoformat()})
        if not page.get("IsTruncated"):
            break
        if page_number >= max_pages:
            next_key, next_version = page.get("NextKeyMarker"), page.get("NextVersionIdMarker")
            validate_cursor(next_key, next_version)
            if next_key is None:
                raise ValueError("Truncated version discovery returned no continuation cursor")
            result.update(truncated=True, next_key_marker=next_key)
            if next_version is not None:
                result["next_version_id_marker"] = next_version
            break
    return result


def _check_restore_space(directory, required_bytes, min_free_bytes, min_free_inodes):
    usage = os.statvfs(directory)
    if usage.f_bavail * usage.f_frsize < required_bytes + min_free_bytes:
        raise ValueError("Insufficient space for the recovery download and reserve")
    if usage.f_favail < min_free_inodes:
        raise ValueError("Insufficient free inodes for recovery")


def download_bundle(client, bucket, prefix, key, destination, manifest_version_id=None, *,
                    max_total_bytes=RESTORE_MAX_BYTES, min_free_bytes=RESTORE_MIN_FREE_BYTES,
                    min_free_inodes=RESTORE_MIN_FREE_INODES):
    for value, minimum in ((max_total_bytes, 1), (min_free_bytes, 0), (min_free_inodes, 0)):
        if type(value) is not int or value < minimum:
            raise ValueError("Recovery limits must be nonnegative integers with a positive size cap")
    validate_key(prefix, key)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Download destination must not exist")
    manifest = read_manifest(client, bucket, key, manifest_version_id)
    total = sum(item["size"] for item in manifest["files"])
    if total > max_total_bytes:
        raise ValueError("Recovery bundle exceeds the configured encrypted size cap")
    base = key.rsplit("/", 1)[0]
    verify_remote(client, bucket, base, manifest)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Include space for checksum sidecars, the manifest and transfer metadata.
    overhead = 10 * 1024**2
    _check_restore_space(destination.parent, total + overhead, min_free_bytes, min_free_inodes)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        progress_lock = threading.Lock()
        last_check = 0.0
        downloaded_bytes = 0

        def check_progress(bytes_delta):
            nonlocal last_check, downloaded_bytes
            # S3 may call this from multiple transfer threads. The initial check
            # reserves the full bundle; subsequent checks catch competing writers.
            with progress_lock:
                # Transfer retries report negative deltas for discarded bytes.
                downloaded_bytes += bytes_delta
                if downloaded_bytes > max_total_bytes:
                    raise ValueError("Recovery transfer exceeded the configured size cap")
                now = time.monotonic()
                if now - last_check >= 0.25:
                    _check_restore_space(stage, overhead, min_free_bytes, min_free_inodes)
                    last_check = now

        remaining = total
        for item in manifest["files"]:
            _check_restore_space(stage, remaining + overhead, min_free_bytes, min_free_inodes)
            path = stage / item["name"]
            arguments = {"VersionId": item["version_id"]} if "version_id" in item else {}
            client.download_file(bucket, base + "/" + item["name"], str(path),
                                 ExtraArgs=arguments, Config=TRANSFER, Callback=check_progress)
            path.chmod(0o600)
            if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
                raise ValueError("Downloaded archive failed integrity verification")
            remaining -= item["size"]
            _check_restore_space(stage, remaining + overhead, min_free_bytes, min_free_inodes)
            # Compatible with the local PostgreSQL restore check.
            (stage / (item["name"] + ".sha256")).write_text(f"{item['sha256']}  {item['name']}\n")
        (stage / "COMPLETE.json").write_text(json.dumps(manifest, indent=2) + "\n")
        stage.rename(destination)
    return {"status": "downloaded_and_checksum_verified", "archives": len(manifest["files"])}


def check_freshness(client, bucket, prefix, max_age_hours, now=None):
    now = now or datetime.now(timezone.utc)
    latest = None
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/daily/"):
        for item in page.get("Contents", []):
            if item["Key"].endswith("/COMPLETE.json"):
                validate_key(prefix, item["Key"])
                manifest = read_manifest(client, bucket, item["Key"])
                snapshot_at = datetime.fromisoformat(manifest["snapshot_at"])
                if latest is None or snapshot_at > latest["snapshot_at"]:
                    latest = {"key": item["Key"], "snapshot_at": snapshot_at, "manifest": manifest}
    if latest is None:
        raise ValueError("No completed external backup exists")
    verify_remote(client, bucket, latest["key"].rsplit("/", 1)[0], latest["manifest"])
    age = (now - latest["snapshot_at"]).total_seconds() / 3600
    if age < -0.25 or age > max_age_hours:
        raise ValueError("Latest completed external backup is stale or has an invalid timestamp")
    return {"status": "fresh", "age_hours": round(age, 2), "completion_key": latest["key"]}


def inspect_recovery_identity(client, bucket, prefix, key, version_id, target):
    """Bounded retained-manifest/sidecar read; never decrypt or download a dump."""
    if target.get("BUCKET") != bucket or target.get("PREFIX") != prefix:
        raise ValueError("Inspection target does not match its storage arguments")
    validate_key(prefix, key)
    version_arguments(version_id)
    manifest = read_manifest(client, bucket, key, version_id)
    lock = manifest.get("object_lock", {})
    if lock.get("mode") != "COMPLIANCE" or datetime.fromisoformat(lock["retain_until"]) <= datetime.now(timezone.utc):
        raise ValueError("Final identity requires unexpired COMPLIANCE retention")
    verify_remote(client, bucket, key.rsplit("/", 1)[0], manifest, verify_content=False)
    items = [item for item in manifest["files"] if item["name"] == IDENTITY_NAME]
    if len(items) != 1:
        raise ValueError("Legacy bundle has no public recovery identity")
    item = items[0]
    result = client.get_object(Bucket=bucket, Key=key.rsplit("/", 1)[0] + "/" + IDENTITY_NAME,
                               **version_arguments(item.get("version_id")))
    stream = result["Body"]
    try:
        verify_version(result, item["version_id"])
        verify_lock(result, lock)
        if result["ContentLength"] != item["size"] or item["size"] > IDENTITY_MAX_BYTES:
            raise ValueError("Remote recovery identity size mismatch")
        raw = stream.read(IDENTITY_MAX_BYTES + 1)
    finally:
        stream.close()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if len(raw) != item["size"] or actual_sha != item["sha256"]:
        raise ValueError("Remote recovery identity content mismatch")
    identity = validate_recovery_identity(json.loads(raw))
    if identity["storage_target"] != target:
        raise ValueError("Recovery identity belongs to another storage target")
    verified_components = {}
    if "components" in identity:
        component = identity["components"]["ogm_vpn"]
        entries = [x for x in manifest["files"] if x["name"] == VPN_ARCHIVE_NAME]
        if len(entries) != 1 or entries[0]["size"] != component["bytes"] or entries[0]["sha256"] != component["sha256"]:
            raise ValueError("Retained VPN ciphertext does not match its public identity")
        item = entries[0]
        remote = client.get_object(Bucket=bucket, Key=key.rsplit("/", 1)[0]+"/"+VPN_ARCHIVE_NAME,
                                   **version_arguments(item["version_id"]))
        body = remote["Body"]
        try:
            verify_version(remote,item["version_id"]); verify_lock(remote,lock)
            if remote["ContentLength"] != item["size"] or item["size"] > VPN_CIPHERTEXT_MAX_BYTES:
                raise ValueError("Retained VPN ciphertext size mismatch")
            ciphertext = body.read(VPN_CIPHERTEXT_MAX_BYTES+1)
        finally:
            body.close()
        if (len(ciphertext) != item["size"] or hashlib.sha256(ciphertext).hexdigest() != item["sha256"]
                or not ciphertext.startswith(b"age-encryption.org/v1\n")):
            raise ValueError("Retained VPN ciphertext integrity mismatch")
        verified_components["ogm_vpn"] = component
    return {"status": "recovery_identity_verified", "storage_target": target,
            "completion_version": {"key": key, "version_id": version_id},
            "remote_manifest": manifest, "recovery_identity": identity,
            "recovery_identity_sha256": actual_sha, "verified_components": verified_components}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("upload").add_argument("directory", type=Path)
    download = commands.add_parser("download")
    download.add_argument("completion_key")
    download.add_argument("destination", type=Path)
    download.add_argument("--manifest-version-id", help="Restore this retained COMPLETE.json version")
    download.add_argument("--max-total-bytes", type=int, default=RESTORE_MAX_BYTES,
                          help="Maximum combined encrypted archive bytes (default: 128 GiB)")
    download.add_argument("--min-free-bytes", type=int, default=RESTORE_MIN_FREE_BYTES,
                          help="Free space preserved during download (default: 40 GiB)")
    download.add_argument("--min-free-inodes", type=int, default=RESTORE_MIN_FREE_INODES)
    versions = commands.add_parser("list-versions", help="Discover current/hidden marker versions with the recovery key")
    versions.add_argument("--group", choices=sorted(RETENTION_DAYS), default="daily")
    versions.add_argument("--max-pages", type=int, default=10)
    versions.add_argument("--page-size", type=int, default=1000)
    versions.add_argument("--key-marker", help="Continue from next_key_marker returned by a bounded listing")
    versions.add_argument("--version-id-marker", help="Continue from the accompanying next_version_id_marker")
    check = commands.add_parser("check")
    check.add_argument("--max-age-hours", type=float, default=30)
    commands.add_parser("preflight")
    identity = commands.add_parser("inspect-identity", help="Read retained public identity without decryption")
    identity.add_argument("completion_key")
    identity.add_argument("--manifest-version-id", required=True)
    args = parser.parse_args()
    try:
        client, bucket, prefix = settings(read_timeout=10, retries=0) if args.operation == "inspect-identity" else settings()
        if args.operation == "upload":
            result = upload_bundle(client, bucket, prefix, args.directory)
        elif args.operation == "download":
            result = download_bundle(client, bucket, prefix, args.completion_key, args.destination,
                                     args.manifest_version_id, max_total_bytes=args.max_total_bytes,
                                     min_free_bytes=args.min_free_bytes, min_free_inodes=args.min_free_inodes)
        elif args.operation == "list-versions":
            result = list_versions(client, bucket, prefix, args.group, args.max_pages, args.page_size,
                                   args.key_marker, args.version_id_marker)
        elif args.operation == "check":
            if not 0 < args.max_age_hours <= 168:
                raise ValueError("Backup freshness threshold must be between 0 and 168 hours")
            result = check_freshness(client, bucket, prefix, args.max_age_hours)
        elif args.operation == "inspect-identity":
            target = public_storage_target(bucket, prefix)
            result = inspect_recovery_identity(client, bucket, prefix, args.completion_key,
                                               args.manifest_version_id, target)
        else:
            client.list_objects_v2(Bucket=bucket, Prefix=prefix + "/daily/", MaxKeys=1)
            result = {"status": "configuration_valid", "connection_tested": True,
                      "write_permission_tested": False}
        print(json.dumps(result))
    except Exception as error:
        # Provider errors may include credential-bearing URLs; keep operational logs redacted.
        print(json.dumps(safe_failure(error, args.operation)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
