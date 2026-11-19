#!/usr/bin/env bash
set -euo pipefail
umask 077

COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"
DB_SERVICE="${DB_SERVICE:-db}"
# Explicit backup selectors are process settings. ENV_FILE's POSTGRES_* values
# do not override the environment of an already-running database container.
BACKUP_POSTGRES_DB="${BACKUP_POSTGRES_DB:-}"
BACKUP_POSTGRES_USER="${BACKUP_POSTGRES_USER:-}"

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

BACKUP_DIR="${BACKUP_DIR:-$(env_file_value BACKUP_DIR)}"
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-$(env_file_value BACKUP_AGE_RECIPIENT)}"
BACKUP_REQUIRE_ENCRYPTION="${BACKUP_REQUIRE_ENCRYPTION:-$(env_file_value BACKUP_REQUIRE_ENCRYPTION)}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-$(env_file_value BACKUP_RETENTION_DAYS)}"
ALLOW_PLAINTEXT_BACKUP="${ALLOW_PLAINTEXT_BACKUP:-$(env_file_value ALLOW_PLAINTEXT_BACKUP)}"
BACKUP_DIR="${BACKUP_DIR:-../map1-turkey-backups}"
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-}"
BACKUP_REQUIRE_ENCRYPTION="${BACKUP_REQUIRE_ENCRYPTION:-0}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
ALLOW_PLAINTEXT_BACKUP="${ALLOW_PLAINTEXT_BACKUP:-0}"

die() {
  echo "$*" >&2
  exit 1
}

[ "$#" -eq 0 ] || die "backup-db.sh takes no arguments"
[[ "$DB_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "invalid DB_SERVICE"
for selector in BACKUP_POSTGRES_DB BACKUP_POSTGRES_USER; do
  value="${!selector}"
  [ -z "$value" ] || [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "$selector must be a plain PostgreSQL identifier of at most 63 characters"
done
[[ "$BACKUP_REQUIRE_ENCRYPTION" =~ ^[01]$ ]] || die "BACKUP_REQUIRE_ENCRYPTION must be 0 or 1"
[[ "$ALLOW_PLAINTEXT_BACKUP" =~ ^[01]$ ]] || die "ALLOW_PLAINTEXT_BACKUP must be 0 or 1"
[[ "$BACKUP_RETENTION_DAYS" =~ ^(0|[1-9][0-9]{0,4})$ ]] || die "invalid BACKUP_RETENTION_DAYS"
for executable in docker sha256sum flock; do
  command -v "$executable" >/dev/null 2>&1 || die "$executable is required"
done

if [ -n "$BACKUP_AGE_RECIPIENT" ]; then
  command -v age >/dev/null 2>&1 || die "age is required when BACKUP_AGE_RECIPIENT is set"
elif [ "$BACKUP_REQUIRE_ENCRYPTION" = 1 ] || [ "$ALLOW_PLAINTEXT_BACKUP" != 1 ]; then
  die "BACKUP_AGE_RECIPIENT is required (plaintext requires explicit ALLOW_PLAINTEXT_BACKUP=1 and BACKUP_REQUIRE_ENCRYPTION=0)"
fi

# Refuse to chmod/prune a working directory, an ancestor, or a symlink.
while [ "$BACKUP_DIR" != / ] && [[ "$BACKUP_DIR" == */ ]]; do
  BACKUP_DIR="${BACKUP_DIR%/}"
done
guard_script="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/backup_volume_guard.py"
# Reads selected guard fields only; never sources ENV_FILE.
python3 -B "$guard_script" --env-file "$ENV_FILE" --directory "$BACKUP_DIR"
[ ! -L "$BACKUP_DIR" ] || die "BACKUP_DIR must not be a symlink"
mkdir -p -m 700 -- "$BACKUP_DIR"
BACKUP_DIR="$(cd -- "$BACKUP_DIR" && pwd -P)"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
for protected_dir in "$(pwd -P)" "$script_dir"; do
  case "$protected_dir/" in
    "$BACKUP_DIR/"*) die "BACKUP_DIR must be a dedicated backup directory" ;;
  esac
done
[ -O "$BACKUP_DIR" ] || die "BACKUP_DIR must be owned by the current user"
chmod 700 -- "$BACKUP_DIR"
[ ! -L "$BACKUP_DIR/.backup.lock" ] || die "backup lock must not be a symlink"
[ ! -e "$BACKUP_DIR/.backup.lock" ] || [ -f "$BACKUP_DIR/.backup.lock" ] || die "invalid backup lock"
exec {lock_fd}>> "$BACKUP_DIR/.backup.lock"
flock -n "$lock_fd" || die "another backup is running"
# Keep the lock inode: deleting a lock file lets concurrent writers lock different files.

staging_dir="$(mktemp -d "$BACKUP_DIR/.backup.XXXXXXXXXXXX")"
checksum_published=0
backup_published=0
cleanup() {
  local status=$?
  trap - EXIT
  if [ "$checksum_published" = 1 ] && [ "$backup_published" = 0 ] && [ ! -f "$backup_file" ]; then
    rm -f -- "$backup_file.sha256"
  fi
  rm -rf -- "$staging_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

timestamp="$(date -u +%Y%m%d-%H%M%S)"
[[ "$timestamp" =~ ^[0-9]{8}-[0-9]{6}$ ]] || die "invalid backup timestamp"
backup_name="map1-turkey-$timestamp-${staging_dir##*.}.dump"
[ -z "$BACKUP_AGE_RECIPIENT" ] || backup_name+=".age"
backup_file="$BACKUP_DIR/$backup_name"
staged_file="$staging_dir/$backup_name"
[ ! -e "$backup_file" ] && [ ! -L "$backup_file" ] || die "backup already exists"
[ ! -e "$backup_file.sha256" ] && [ ! -L "$backup_file.sha256" ] || die "backup checksum already exists"

dump_database() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
    sh -ec 'pg_dump --no-tablespaces -U "${1:-$POSTGRES_USER}" -d "${2:-$POSTGRES_DB}" -Fc' \
    sh "$BACKUP_POSTGRES_USER" "$BACKUP_POSTGRES_DB"
}

check_dump_header() {
  local magic
  IFS= read -r -N 5 magic || die "empty or truncated pg_dump output"
  [ "$magic" = PGDMP ] || die "pg_dump output is not a custom archive"
  printf '%s' "$magic"
  cat
}

if [ -n "$BACKUP_AGE_RECIPIENT" ]; then
  dump_database | check_dump_header | python3 -B "$guard_script" --env-file "$ENV_FILE" \
    --directory "$BACKUP_DIR" --writer -- age -r "$BACKUP_AGE_RECIPIENT" -o "$staged_file"
else
  dump_database | check_dump_header | python3 -B "$guard_script" --env-file "$ENV_FILE" \
    --directory "$BACKUP_DIR" --writer -- sh -c 'cat > "$1"' sh "$staged_file"
fi
[ -s "$staged_file" ] || die "backup is empty"
chmod 600 -- "$staged_file"
(
  cd -- "$staging_dir"
  sha256sum -- "$backup_name" > "$backup_name.sha256"
  sha256sum --strict --status -c "$backup_name.sha256"
)
chmod 600 -- "$staged_file.sha256"

# The archive rename is the publish point. Its complete checksum is already in place.
# Both renames stay on the same filesystem; unfinished work has no final archive name.
checksum_published=1
mv -T -- "$staged_file.sha256" "$backup_file.sha256"
mv -T -- "$staged_file" "$backup_file"
backup_published=1

if [ "$BACKUP_RETENTION_DAYS" != 0 ]; then
  # Only our timestamped archives, in this directory, with their matching sidecars.
  # Prune failure must not disguise an already completed backup as a failed dump.
  if ! find "$BACKUP_DIR" -maxdepth 1 -regextype posix-extended -type f \
    -regex '.*/map1-turkey-[0-9]{8}-[0-9]{6}(-[A-Za-z0-9]{12})?\.dump(\.age)?' \
    ! -name "$backup_name" -mtime +"$BACKUP_RETENTION_DAYS" \
    -exec rm -f -- '{}' '{}.sha256' \; >&2; then
    echo "warning: backup completed, but retention pruning failed" >&2
  fi
fi

# Machine-readable stdout for backup-offsite.sh. Send all diagnostics to stderr.
printf '%s\n' "$backup_file"
