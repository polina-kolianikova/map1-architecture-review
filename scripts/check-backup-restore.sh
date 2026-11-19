#!/usr/bin/env bash
set -euo pipefail
umask 077

# Deliberately no production target defaults. This changes restore checks only;
# backup-db.sh and scheduled main backups retain their existing defaults.
COMPOSE_FILE="${COMPOSE_FILE:-}"
ENV_FILE="${ENV_FILE:-.env}"
DB_SERVICE="${DB_SERVICE:-}"
BACKUP_FILE="${1:-${BACKUP_FILE:-}}"

if [ "${1:-}" = --help ]; then
  cat <<'HELP'
Usage: scripts/check-backup-restore.sh ARCHIVE.dump[.age]

Start a dedicated PostgreSQL restore service with its own empty data volume.
Its service labels must include: io.map1.restore-isolated: "true"
Never apply that label to the live service or share its PGDATA/tablespaces.
An isolated service may be named db, but its Compose project must be explicit.

Required process settings:
  COMPOSE_FILE=/path/to/isolated-compose.yml
  RESTORE_COMPOSE_PROJECT=map1-restore
  DB_SERVICE=restore-db
  RESTORE_EXPECTED_CONTAINER_ID=<full 64-character container ID>
  ENV_FILE=/path/to/isolated-runtime.env

Optional RESTORE_COMPOSE_OVERRIDE_FILE adds a second -f file, for example an
override defining only the dedicated restore service, isolated volume and label.
Obtain the expected ID from the inspected isolated deployment, not from a live
service. The checker verifies project/service/label/running identity, then pins
every exec (including cleanup) to that ID and uses the local PostgreSQL socket.
BACKUP_AGE_IDENTITY is required for encrypted archives. Size the isolated volume
for the expanded restore, indexes, WAL and reserve before running this check.
HELP
  exit 0
fi

die() {
  echo "$*" >&2
  exit 1
}

env_file_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  awk -F= -v key="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == key {
      sub(/^[^=]*=/, "")
      sub(/\r$/, "")
      print
      exit
    }
  ' "$ENV_FILE"
}

BACKUP_AGE_IDENTITY="${BACKUP_AGE_IDENTITY:-$(env_file_value BACKUP_AGE_IDENTITY)}"
[ "$#" -le 1 ] && [ -n "$BACKUP_FILE" ] || die "usage: scripts/check-backup-restore.sh path/to/backup.dump[.age]"
[ -n "$COMPOSE_FILE" ] && [ -n "$DB_SERVICE" ] || die "explicit isolated COMPOSE_FILE and DB_SERVICE required; see --help"
[[ "${RESTORE_COMPOSE_PROJECT:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "explicit RESTORE_COMPOSE_PROJECT required; see --help"
[[ "${RESTORE_EXPECTED_CONTAINER_ID:-}" =~ ^[a-f0-9]{64}$ ]] || die "RESTORE_EXPECTED_CONTAINER_ID must be the full isolated container ID"
[[ "$DB_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "invalid DB_SERVICE"
[ -f "$BACKUP_FILE" ] && [ -s "$BACKUP_FILE" ] && [ ! -L "$BACKUP_FILE" ] || die "backup must be a nonempty regular file, not a symlink"
BACKUP_FILE="$(cd -- "$(dirname -- "$BACKUP_FILE")" && pwd -P)/$(basename -- "$BACKUP_FILE")"
backup_name="${BACKUP_FILE##*/}"
[[ "$backup_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.dump(\.age)?$ ]] || die "invalid backup filename"
checksum_file="$BACKUP_FILE.sha256"
[ -f "$checksum_file" ] && [ ! -L "$checksum_file" ] || die "backup checksum is missing or unsafe: $checksum_file"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v docker >/dev/null 2>&1 || die "docker is required"

# Never trust filenames supplied by a checksum manifest. Accept exactly one entry,
# bound to this archive, then hash the archive itself (not any manifest path).
mapfile -t checksum_lines < "$checksum_file"
[ "${#checksum_lines[@]}" -eq 1 ] || die "checksum must contain exactly one entry"
[[ "${checksum_lines[0]}" =~ ^([[:xdigit:]]{64})\ [\ *](.+)$ ]] || die "malformed backup checksum"
expected_hash="${BASH_REMATCH[1],,}"
[ "${BASH_REMATCH[2]}" = "$backup_name" ] || die "checksum filename does not match the backup"
actual_hash="$(sha256sum -- "$BACKUP_FILE")"
[ "${actual_hash:0:64}" = "$expected_hash" ] || die "backup checksum mismatch"

compose() {
  local overrides=()
  if [ -n "${RESTORE_COMPOSE_OVERRIDE_FILE:-}" ]; then
    overrides=(-f "$RESTORE_COMPOSE_OVERRIDE_FILE")
  fi
  docker compose --project-name "$RESTORE_COMPOSE_PROJECT" --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" "${overrides[@]}" "$@"
}

# Resolve exactly one running, explicitly labelled service. Never re-resolve the
# service name after this check: a replacement container must not receive writes
# or the cleanup for an earlier invocation.
container_id="$(compose ps --all -q "$DB_SERVICE")" || die "cannot resolve isolated restore service"
[ "$container_id" = "$RESTORE_EXPECTED_CONTAINER_ID" ] || die "isolated restore container identity mismatch"
identity="$(docker inspect --format '{{.Id}}|{{.State.Running}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "io.map1.restore-isolated"}}' "$container_id")" \
  || die "cannot inspect isolated restore service"
[ "$identity" = "$container_id|true|$RESTORE_COMPOSE_PROJECT|$DB_SERVICE|true" ] \
  || die "restore service must be running and match the isolated project/service/label"

target_exec() {
  docker exec -i "$container_id" "$@"
}

archive_stream() {
  case "$BACKUP_FILE" in
    *.age) age -d -i "$BACKUP_AGE_IDENTITY" "$BACKUP_FILE" ;;
    *) cat -- "$BACKUP_FILE" ;;
  esac
}

case "$BACKUP_FILE" in
  *.age)
    [ -n "$BACKUP_AGE_IDENTITY" ] && [ -f "$BACKUP_AGE_IDENTITY" ] && [ -r "$BACKUP_AGE_IDENTITY" ] || die "BACKUP_AGE_IDENTITY must be a readable identity file"
    command -v age >/dev/null 2>&1 || die "age is required for encrypted backup restore checks"
    ;;
esac

# Read the TOC and drain the entire stream so late age authentication failures are
# detected before createdb. No decrypted archive is written to local disk.
archive_stream | target_exec sh -ec \
  'pg_restore --list >/dev/null; cat >/dev/null'

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
validation_sql="$script_dir/backup-restore-validation.sql"
[ -r "$validation_sql" ] || die "restore validation SQL is missing"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/map1-restore-check.XXXXXXXXXXXX")"
restore_db="map1_restore_check_${work_dir##*.}"
[[ "$restore_db" =~ ^map1_restore_check_[A-Za-z0-9]{12}$ ]] || die "invalid restore database name"
db_created=0

drop_restore_db() {
  target_exec sh -ec \
    'dropdb -h /var/run/postgresql -p 5432 -U "$POSTGRES_USER" --maintenance-db=postgres --if-exists -- "$1"' sh "$restore_db" || return $?
  db_created=0
}

cleanup() {
  local status=$?
  trap - EXIT
  if [ "$db_created" = 1 ]; then
    if ! drop_restore_db; then
      echo "failed to remove temporary restore database: $restore_db" >&2
      status=1
    fi
  fi
  rm -rf -- "$work_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Only drop a database that this invocation successfully created; a create failure
# (including a name collision) never authorizes removal of an existing database.
target_exec sh -ec \
  'createdb -h /var/run/postgresql -p 5432 -U "$POSTGRES_USER" --maintenance-db=postgres --template=template0 -- "$1"' sh "$restore_db"
db_created=1

archive_stream | target_exec sh -ec \
  'pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --no-tablespaces -h /var/run/postgresql -p 5432 -U "$POSTGRES_USER" -d "$1"' sh "$restore_db"

target_exec sh -ec \
  'psql -X -h /var/run/postgresql -p 5432 -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 -At' sh "$restore_db" < "$validation_sql"

drop_restore_db
echo "backup restore check passed (archive schema and audit guards; no source row-count comparison)"
