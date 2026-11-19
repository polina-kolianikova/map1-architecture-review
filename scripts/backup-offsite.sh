#!/usr/bin/env bash
set -euo pipefail
umask 077

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
ENV_FILE="${ENV_FILE:-.env}"
BACKUP_DIR="${BACKUP_DIR:-/srv/map1/backups}"
: "${BACKUP_S3_ENV_FILE:?Set the private independent S3 environment file}"
: "${BACKUP_SOURCE_ARCHIVE:?Set the deployed source archive path}"
: "${BACKUP_AGE_RECIPIENT:?Set the public age encryption recipient}"
: "${BACKUP_MAX_DUMP_GB:?Set a measured maximum encrypted database archive size}"
BACKUP_MIN_FREE_GB="${BACKUP_MIN_FREE_GB:-40}"
# Host-side admission before any mkdir, staging, transfer or provider call.
python3 -B "$project_dir/scripts/backup_volume_guard.py" --env-file "$ENV_FILE" --directory "$BACKUP_DIR"
# Optional newline-separated absolute HOST directories (spaces are literal).
# Example in a shell: BACKUP_RESERVE_PATHS=$'/srv/map1/storage\n/var/lib/docker'
# Leave unset for the existing archive-only reserve behavior. Future production
# must list PGDATA, external tablespaces/WAL and root/Docker filesystems as needed.
# All paths share BACKUP_MIN_FREE_GB/BACKUP_MIN_FREE_INODES; this is not a UUID gate.
reserve_args=()
while IFS= read -r reserve_path || [ -n "$reserve_path" ]; do
  [ -n "$reserve_path" ] || continue
  if [[ "$reserve_path" != /* ]] || [ ! -d "$reserve_path" ]; then
    echo 'BACKUP_RESERVE_PATHS requires existing absolute host directories' >&2
    exit 1
  fi
  reserve_args+=(--reserve-path "$reserve_path")
done <<< "${BACKUP_RESERVE_PATHS:-}"
if [ -n "${BACKUP_EXTRA_RESERVE_PATH:-}" ]; then
  reserve_args+=(--reserve-path "$BACKUP_EXTRA_RESERVE_PATH")
fi
[ ! -L "$BACKUP_DIR" ] || { echo 'Backup directory must not be a symbolic link' >&2; exit 1; }

[[ "$BACKUP_MIN_FREE_GB" =~ ^(0|[1-9][0-9]{0,4})$ ]] || { echo 'BACKUP_MIN_FREE_GB must be an integer below 100000' >&2; exit 1; }
[[ "$BACKUP_MAX_DUMP_GB" =~ ^[1-9][0-9]{0,4}$ ]] || { echo 'BACKUP_MAX_DUMP_GB must be an integer from 1 to 99999' >&2; exit 1; }
for input in "$ENV_FILE" "$BACKUP_SOURCE_ARCHIVE" "$BACKUP_S3_ENV_FILE"; do
  if [ ! -f "$input" ] || [ -L "$input" ] || [ ! -s "$input" ] || [ ! -r "$input" ]; then
    echo 'A required recovery configuration file is missing, unreadable, empty or a symbolic link' >&2
    exit 1
  fi
done
for input in "${BACKUP_SOURCE_MANIFEST:-}" "${BACKUP_DEPLOY_OVERRIDE_FILE:-}" "${BACKUP_JOB_ENV_FILE:-}"; do
  [ -n "$input" ] || continue
  [ -f "$input" ] && [ ! -L "$input" ] && [ -s "$input" ] && [ -r "$input" ] || { echo 'An optional recovery configuration file is missing, unreadable, empty or unsafe' >&2; exit 1; }
done
vpn_capture="${BACKUP_OGM_VPN_CAPTURE:-0}"
[[ "$vpn_capture" =~ ^[01]$ ]] || { echo 'VPN capture must be 0 or 1' >&2; exit 1; }
if [ -n "${BACKUP_OGM_VPN_ARCHIVE:-}${BACKUP_OGM_VPN_SHA256:-}${BACKUP_OGM_VPN_VERIFICATION:-}" ]; then
  echo 'Historical VPN input is not current capture; use BACKUP_OGM_VPN_CAPTURE and BUILD_DIR' >&2; exit 1
fi
if [ "$vpn_capture" = 1 ]; then
  : "${BACKUP_OGM_VPN_BUILD_DIR:?Set the current VPN build directory with Dockerfile, .dockerignore and supervisor.py}"
  [ -n "${BACKUP_SOURCE_MANIFEST:-}" ] || { echo 'VPN capture requires the source manifest' >&2; exit 1; }
  [ -d "$BACKUP_OGM_VPN_BUILD_DIR" ] && [ ! -L "$BACKUP_OGM_VPN_BUILD_DIR" ] || { echo 'Invalid VPN build directory' >&2; exit 1; }
fi
notification_files=()
processing_notification=""
if [ -n "${BACKUP_NOTIFICATION_ENV_DIR:-}" ]; then
  [ -d "$BACKUP_NOTIFICATION_ENV_DIR" ] && [ ! -L "$BACKUP_NOTIFICATION_ENV_DIR" ] || { echo 'Invalid notification configuration directory' >&2; exit 1; }
  for name in backup-alert-map1-offsite-backup.service.env backup-alert-map1-offsite-backup-check.service.env; do
    input="$BACKUP_NOTIFICATION_ENV_DIR/$name"
    [ -f "$input" ] && [ ! -L "$input" ] && [ -s "$input" ] && [ -r "$input" ] || { echo 'A required notification recovery configuration is missing, unreadable or unsafe' >&2; exit 1; }
    notification_files+=("$input")
  done
  input="$BACKUP_NOTIFICATION_ENV_DIR/processing-monitor.env"
  if [ -e "$input" ] || [ -L "$input" ]; then
    python3 - "$input" <<'PY'
import os, stat, sys
info = os.lstat(sys.argv[1])
if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
        or not 0 < info.st_size <= 65536):
    raise SystemExit('Unsafe processing notification recovery configuration')
PY
    processing_notification="$input"
  fi
fi
command -v age >/dev/null
command -v flock >/dev/null
command -v python3 >/dev/null
verify_release() {
  python3 - "$1" "$2" <<'PY'
import hashlib, json, sys
with open(sys.argv[1]) as source:
    expected = json.load(source).get('source_sha256')
value = hashlib.sha256()
with open(sys.argv[2], 'rb') as source:
    for block in iter(lambda: source.read(1024 * 1024), b''):
        value.update(block)
if not isinstance(expected, str) or expected != value.hexdigest():
    raise SystemExit('Deployed source archive does not match its release manifest')
PY
}
verify_explicit_target() {
  python3 - "$1" "${BACKUP_POSTGRES_DB:-}" "${BACKUP_POSTGRES_USER:-}" <<'PY'
import re, sys
from pathlib import Path
values = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    key, separator, value = line.partition('=')
    key = key.strip()
    if not separator or key not in ('POSTGRES_DB', 'POSTGRES_USER'):
        continue
    if key in values:
        raise SystemExit('Explicit backup target requires unambiguous runtime database settings')
    value = value.strip()
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        value = value[1:-1]
    values[key] = value
resolved = []
for key, override in zip(('POSTGRES_DB', 'POSTGRES_USER'), sys.argv[2:]):
    configured = values.get(key, '')
    selected = override or configured
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', selected):
        raise SystemExit('Explicit backup target requires plain PostgreSQL identifiers')
    if selected != configured:
        raise SystemExit('Explicit backup target must match the recovery runtime environment')
    resolved.append(selected)
print('\n'.join(resolved))
PY
}
explicit_target=''
if [ -n "${BACKUP_POSTGRES_DB:-}" ] || [ -n "${BACKUP_POSTGRES_USER:-}" ]; then
  [ -n "${BACKUP_SOURCE_MANIFEST:-}" ] || { echo 'An explicit backup target requires its source manifest' >&2; exit 1; }
  explicit_target="$(verify_explicit_target "$ENV_FILE")"
  export BACKUP_POSTGRES_DB="${explicit_target%%$'\n'*}"
  export BACKUP_POSTGRES_USER="${explicit_target#*$'\n'}"
fi
if [ -n "${BACKUP_SOURCE_MANIFEST:-}" ]; then
  verify_release "$BACKUP_SOURCE_MANIFEST" "$BACKUP_SOURCE_ARCHIVE"
fi
age -r "$BACKUP_AGE_RECIPIENT" </dev/null >/dev/null
mkdir -p -m 700 "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
[ -O "$BACKUP_DIR" ] || { echo 'Backup directory must be owned by the current user' >&2; exit 1; }
case "$project_dir/" in "$BACKUP_DIR/"*) echo 'Use a dedicated backup directory' >&2; exit 1 ;; esac
chmod 700 "$BACKUP_DIR"
[ ! -L "$BACKUP_DIR/.offsite.lock" ] || { echo 'Invalid backup lock' >&2; exit 1; }
exec 9>"$BACKUP_DIR/.offsite.lock"
flock -n 9 || { echo 'An external backup is already running' >&2; exit 1; }

# A retained bundle can belong to another target/profile. Default scheduled jobs
# still retry retained work; explicit jobs must resolve it separately, never
# republish an unidentified snapshot into a different recovery namespace.
if [ -n "$explicit_target" ]; then
  for pending in "$BACKUP_DIR"/bundle-*; do
    [ -d "$pending" ] && [ ! -L "$pending" ] && [ -O "$pending" ] || continue
    [[ "${pending##*/}" =~ ^bundle-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}$ ]] || continue
    [ -e "$pending/.offsite-verified.json" ] || { echo 'Resolve retained unverified work before an explicit-target backup' >&2; exit 1; }
  done
fi

export BACKUP_UID="${BACKUP_UID:-$(id -u)}"
export BACKUP_GID="${BACKUP_GID:-$(id -g)}"
export BACKUP_BUNDLE_DIR="$BACKUP_DIR"
export BACKUP_RESTORE_DIR="${BACKUP_RESTORE_DIR:-$BACKUP_DIR/restore}"
python3 -B "$project_dir/scripts/backup_volume_guard.py" --env-file "$ENV_FILE" --directory "$BACKUP_RESTORE_DIR"
mkdir -p -m 700 "$BACKUP_RESTORE_DIR"
transfer() {
  docker compose --project-name map1-backup --env-file "$ENV_FILE" \
    -f infra/docker-compose.backup.yml run --rm --no-deps backup-transfer "$@"
}

upload_verified() {
  local current="$1"
  export BACKUP_BUNDLE_DIR="$current"
  if ! transfer upload /bundles > "$current/.upload-receipt.partial"; then
    echo "External transfer failed; encrypted recovery bundle retained at $current" >&2
    return 1
  fi
  python3 - "$current/.upload-receipt.partial" <<'PY'
import json, sys
with open(sys.argv[1]) as source:
    receipt = json.load(source)
if receipt.get('status') != 'uploaded' or receipt.get('remote_content_verified') is not True or not receipt.get('completion_keys'):
    raise SystemExit('External content verification receipt is missing')
PY
  mv "$current/.upload-receipt.partial" "$current/.offsite-verified.json"
}

prune_verified() {
  local current="$1" previous
  for previous in "$BACKUP_DIR"/bundle-*; do
    [ "$previous" != "$current" ] || continue
    [ -d "$previous" ] && [ ! -L "$previous" ] && [ -O "$previous" ] || continue
    [[ "${previous##*/}" =~ ^bundle-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}$ ]] || continue
    [ -f "$previous/.offsite-verified.json" ] && [ ! -L "$previous/.offsite-verified.json" ] || continue
    rm -rf -- "$previous"
  done
}

# Reject missing storage configuration before starting a potentially large dump.
transfer preflight
# Retry retained work first. A storage outage must not create another full dump
# every day. Source timestamps survive retry and cannot make an old snapshot fresh.
for pending in "$BACKUP_DIR"/bundle-*; do
  [ -d "$pending" ] && [ ! -L "$pending" ] && [ -O "$pending" ] || continue
  [[ "${pending##*/}" =~ ^bundle-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}$ ]] || continue
  [ ! -e "$pending/.offsite-verified.json" ] || continue
  upload_verified "$pending"
done

config_bytes="$(stat -c %s -- "$ENV_FILE")"
config_bytes="$((config_bytes + $(stat -c %s -- "$BACKUP_SOURCE_ARCHIVE")))"
config_bytes="$((config_bytes + $(stat -c %s -- "$BACKUP_S3_ENV_FILE")))"
if [ "$vpn_capture" = 1 ]; then
  config_bytes="$((config_bytes + 1048576))"
fi
for input in "${notification_files[@]}"; do
  config_bytes="$((config_bytes + $(stat -c %s -- "$input")))"
done
if [ -n "$processing_notification" ]; then
  config_bytes="$((config_bytes + 65536))"
fi
for input in "${BACKUP_SOURCE_MANIFEST:-}" "${BACKUP_DEPLOY_OVERRIDE_FILE:-}" "${BACKUP_JOB_ENV_FILE:-}"; do
  [ -n "$input" ] || continue
  config_bytes="$((config_bytes + $(stat -c %s -- "$input")))"
done
if [ -n "${BACKUP_EXTRA_CONFIG_FILE:-}" ]; then
  [ -f "$BACKUP_EXTRA_CONFIG_FILE" ] && [ ! -L "$BACKUP_EXTRA_CONFIG_FILE" ] || { echo 'Invalid extra project configuration file' >&2; exit 1; }
  config_bytes="$((config_bytes + $(stat -c %s -- "$BACKUP_EXTRA_CONFIG_FILE")))"
fi
free_kb="$(df -Pk "$BACKUP_DIR" | awk 'NR == 2 { print $4 }')"
required_kb="$(((BACKUP_MIN_FREE_GB + BACKUP_MAX_DUMP_GB) * 1024 * 1024 + config_bytes * 3 / 1024 + 10240))"
if [ "$free_kb" -lt "$required_kb" ]; then
  echo 'Insufficient space for the archive size cap, configuration and database reserve' >&2
  exit 1
fi
stage="$(mktemp -d "$BACKUP_DIR/.bundle-XXXXXXXX")"
cleanup() {
  if [ -n "${stage:-}" ] && [ -d "$stage" ]; then rm -rf -- "$stage"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Freeze recovery inputs before the dump so a concurrent deployment cannot change
# the source/configuration pair that will be encrypted with this snapshot.
mkdir -m 700 "$stage/configuration"
install -m 600 "$ENV_FILE" "$stage/configuration/runtime.env"
install -m 600 "$BACKUP_SOURCE_ARCHIVE" "$stage/configuration/source.tar.gz"
install -m 600 "$BACKUP_S3_ENV_FILE" "$stage/configuration/backup-s3.env"
if [ -n "${BACKUP_SOURCE_MANIFEST:-}" ]; then
  install -m 600 "$BACKUP_SOURCE_MANIFEST" "$stage/configuration/source-manifest.json"
fi
if [ -n "${BACKUP_DEPLOY_OVERRIDE_FILE:-}" ]; then
  install -m 600 "$BACKUP_DEPLOY_OVERRIDE_FILE" "$stage/configuration/deploy.override.json"
fi
if [ -n "${BACKUP_JOB_ENV_FILE:-}" ]; then
  install -m 600 "$BACKUP_JOB_ENV_FILE" "$stage/configuration/backup-job.env"
fi
for input in "${notification_files[@]}"; do
  install -m 600 "$input" "$stage/configuration/${input##*/}"
done
if [ -n "$processing_notification" ]; then
  python3 - "$processing_notification" "$stage/configuration/processing-monitor.env" <<'PY'
import os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    before = os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
            or not 0 < before.st_size <= 65536):
        raise SystemExit('Unsafe processing notification recovery configuration')
    raw = os.read(fd, 65537)
    after = os.fstat(fd)
    if (len(raw) != before.st_size or len(raw) > 65536
            or (before.st_uid, before.st_mode, before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_uid, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise SystemExit('Processing notification changed during recovery capture')
finally:
    os.close(fd)
target = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(target, 'wb') as output:
    os.fchmod(output.fileno(), 0o600)
    output.write(raw)
    output.flush()
    os.fsync(output.fileno())
PY
fi
if [ -n "${BACKUP_EXTRA_CONFIG_FILE:-}" ]; then
  if [ ! -f "$BACKUP_EXTRA_CONFIG_FILE" ] || [ -L "$BACKUP_EXTRA_CONFIG_FILE" ]; then
    echo 'The configured extra project file is not a regular file' >&2
    exit 1
  fi
  install -m 600 "$BACKUP_EXTRA_CONFIG_FILE" "$stage/configuration/project-source-access.conf"
fi
if [ -n "${BACKUP_SOURCE_MANIFEST:-}" ]; then
  verify_release "$stage/configuration/source-manifest.json" "$stage/configuration/source.tar.gz"
fi
if [ -n "$explicit_target" ]; then
  [ "$(verify_explicit_target "$stage/configuration/runtime.env")" = "$explicit_target" ] || { echo 'Recovery runtime target changed before snapshot' >&2; exit 1; }
fi
# Derive public identity from the frozen inputs, before dump/encryption. Legacy
# jobs without a source manifest remain readable but cannot pass final delivery.
if [ "$vpn_capture" = 1 ]; then
  python3 -B "$project_dir/scripts/backup_offsite.py" freeze-vpn-current \
    "$BACKUP_OGM_VPN_BUILD_DIR" "$stage/configuration"
  # Member paths/digests and restore instructions remain inside this archive.
  tar -C "$stage/configuration/vpn" -czf - . \
    | age -r "$BACKUP_AGE_RECIPIENT" -o "$stage/ogm-vpn-recovery.tar.age.partial"
  test -s "$stage/ogm-vpn-recovery.tar.age.partial"
  mv "$stage/ogm-vpn-recovery.tar.age.partial" "$stage/ogm-vpn-recovery.tar.age"
  rm -rf -- "$stage/configuration/vpn"
fi
if [ -n "${BACKUP_SOURCE_MANIFEST:-}" ]; then
  # Pin the dump's actual -d/-U arguments to this same frozen runtime. Relying
  # on a running container's defaults could otherwise contradict the sidecar.
  frozen_target="$(verify_explicit_target "$stage/configuration/runtime.env")"
  export BACKUP_POSTGRES_DB="${frozen_target%%$'\n'*}"
  export BACKUP_POSTGRES_USER="${frozen_target#*$'\n'}"
  python3 -B "$project_dir/scripts/backup_offsite.py" build-identity \
    "$stage/configuration" "$stage/recovery-identity.json"
fi
date -u +%Y-%m-%dT%H:%M:%S+00:00 > "$stage/snapshot-at.txt"
BACKUP_DIR="$stage" BACKUP_RETENTION_DAYS=0 BACKUP_REQUIRE_ENCRYPTION=1 \
  ENV_FILE="$stage/configuration/runtime.env" python3 "$project_dir/scripts/backup_budget.py" \
    --directory "$stage" \
    "${reserve_args[@]}" \
    --min-free-bytes "$((BACKUP_MIN_FREE_GB * 1024 * 1024 * 1024))" \
    --min-free-inodes "${BACKUP_MIN_FREE_INODES:-100000}" \
    --max-file-bytes "$((BACKUP_MAX_DUMP_GB * 1024 * 1024 * 1024))" \
    -- "$project_dir/scripts/backup-db.sh"
tar -C "$stage/configuration" -czf - . \
  | python3 -B "$project_dir/scripts/backup_volume_guard.py" \
      --env-file "$stage/configuration/runtime.env" --directory "$stage" --writer -- \
      age -r "$BACKUP_AGE_RECIPIENT" -o "$stage/configuration.tar.age.partial"
test -s "$stage/configuration.tar.age.partial"
mv "$stage/configuration.tar.age.partial" "$stage/configuration.tar.age"
rm -rf -- "$stage/configuration"
bundle="$BACKUP_DIR/bundle-$(date -u +%Y%m%dT%H%M%SZ)-${stage##*.bundle-}"
mv "$stage" "$bundle"
stage=''
upload_verified "$bundle"
# Keep the latest verified local bundle. Failed/unverified transfers are retained.
# S3 retention is administered independently; the server has no delete permission.
prune_verified "$bundle"
echo "Encrypted external recovery bundle verified; local copy retained at $bundle"
