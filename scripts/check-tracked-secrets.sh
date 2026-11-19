#!/usr/bin/env sh
set -eu

CHECK_LOCAL_SECRETS="${CHECK_LOCAL_SECRETS:-0}"
SCANNER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

status=0

secret_pattern='(BEGIN (RSA|OPENSSH|PRIVATE) KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]+|sk-[A-Za-z0-9_-]{20,}|pk\.eyJ[A-Za-z0-9._-]{20,}|sk\.eyJ[A-Za-z0-9._-]{20,})'
hash_pattern='caddy_basic_auth_hash[[:space:]]*=[[:space:]]*['"'"'"]?(\$2[aby]\$|argon2(id|i|d)\$)'
assignment_pattern="^[[:space:]]*(export[[:space:]]+)?[A-Za-z0-9_]*(password|token|api_key|secret|client_secret|private_key|caddy_basic_auth_hash)[A-Za-z0-9_]*[[:space:]]*=[[:space:]]*['\"][^'\"\\$]{6,}['\"]"
plain_assignment_pattern="^[[:space:]]*(export[[:space:]]+)?[A-Za-z0-9_]*(password|token|api_key|secret|client_secret|private_key|caddy_basic_auth_hash)[A-Za-z0-9_]*[[:space:]]*=[[:space:]]*['\"]?[A-Za-z0-9_+/@%=-]{10,}"
credential_url_pattern='://[^/@:[:space:]]+:[^/@[:space:]]+@'

filter_source_false_positives() {
  case "$1" in
    'plain credential assignment'|'credential URL')
      # Candidate contents stay in the helper process; only paths are printed.
      # Missing Python/helper or parse/read errors must never silently pass.
      python3 "$SCANNER_DIR/filter-secret-scan-candidates.py" "$1" "$2"
      ;;
    *) cat ;;
  esac
}

check_tracked_pattern() {
  label="$1"
  pattern="$2"
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi
  matches="$(
    git grep -I -i -l -E "$pattern" -- . \
      ':(exclude).env.example' \
      ':(exclude)scripts/check-tracked-secrets.sh' \
      ':(exclude)scripts/filter-secret-scan-candidates.py' \
      || true
  )"
  matches="$(printf '%s\n' "$matches" | filter_source_false_positives "$label" "$pattern")"
  if [ -n "$matches" ]; then
    echo "FAIL tracked secret pattern: $label"
    echo "$matches" | sed 's/^/  /'
    status=1
  fi
}

check_local_pattern() {
  label="$1"
  pattern="$2"
  if command -v rg >/dev/null 2>&1; then
    matches="$(
      rg -I -i -l --hidden --no-ignore -e "$pattern" . \
        --glob '!.git/**' \
        --glob '!frontend/node_modules/**' \
        --glob '!frontend/dist/**' \
        --glob '!frontend/test-results/**' \
        --glob '!frontend/playwright-report/**' \
        --glob '!backend/**/__pycache__/**' \
        --glob '!*.pdf' \
        --glob '!.env.example' \
        --glob '!scripts/check-tracked-secrets.sh' \
        --glob '!scripts/filter-secret-scan-candidates.py' \
        || true
    )"
  else
    matches="$(
      find . \
        \( -path './.git' -o -path './frontend/node_modules' -o -path './frontend/dist' -o -path './frontend/test-results' -o -path './frontend/playwright-report' -o -path './backend/*/__pycache__' \) -prune -o \
        -type f \
        ! -name '*.pdf' \
        ! -name '.env.example' \
        ! -path './scripts/check-tracked-secrets.sh' \
        ! -path './scripts/filter-secret-scan-candidates.py' \
        -exec grep -I -i -l -E "$pattern" {} + \
        2>/dev/null \
        || true
    )"
  fi
  matches="$(printf '%s\n' "$matches" | filter_source_false_positives "$label" "$pattern")"
  if [ -n "$matches" ]; then
    echo "FAIL local secret pattern: $label"
    echo "$matches" | sed 's#^\./##; s/^/  /'
    status=1
  fi
}

check_sensitive_paths() {
  matches="$(
    find . \
      \( -path './.git' -o -path './frontend/node_modules' -o -path './frontend/dist' \) -prune -o \
      -type f \
      \( \
        -name '.env' -o \
        -name '.env.*' -o \
        -path './client_context/*access*' -o \
        -path './client_context/*demo*access*' -o \
        -path './client_context/*данные*' -o \
        -path './data/raw/*' -o \
        -path './backups/*' -o \
        -name '*.dump' -o \
        -name '*.dump.age' -o \
        -name '*.pem' -o \
        -name '*.key' \
      \) \
      ! -name '.env.example' \
      -print \
      || true
  )"
  if [ -n "$matches" ]; then
    echo "FAIL sensitive local files live inside the repository root"
    echo "$matches" | sed 's#^\./##; s/^/  /'
    status=1
  fi
}

check_tracked_sensitive_paths() {
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi
  matches="$(
    git ls-files | awk '
      $0 == ".env" { print; next }
      $0 ~ /^\.env\./ && $0 != ".env.example" { print; next }
      $0 ~ /^client_context\// { print; next }
      $0 ~ /^data\/raw\// { print; next }
      $0 ~ /^backups\// { print; next }
      $0 ~ /\.(dump|dump\.age|pem|key)$/ { print; next }
    '
  )"
  if [ -n "$matches" ]; then
    echo "FAIL sensitive tracked paths"
    echo "$matches" | sed 's/^/  /'
    status=1
  fi
}

check_client_context_absent() {
  matches="$(
    find ./client_context -type f -print 2>/dev/null || true
  )"
  if [ -n "$matches" ]; then
    echo "FAIL client_context files must stay outside the repository root"
    echo "$matches" | sed 's#^\./##; s/^/  /'
    status=1
  fi
}

check_env_example_placeholders() {
  if [ ! -f .env.example ]; then
    return 0
  fi
  bad_keys="$(
    awk -F= '
      /^[[:space:]]*#/ { next }
      /^[[:space:]]*$/ { next }
      {
        key=tolower($1)
        value=$0
        sub(/^[^=]*=/, "", value)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        if (key ~ /(password|token|api_key|secret|client_secret|private_key|caddy_basic_auth_hash)/) {
          if (value != "" && value !~ /^change-me/ && value !~ /^<[A-Z0-9_]+>$/) {
            print $1
          }
        }
      }
    ' .env.example
  )"
  if [ -n "$bad_keys" ]; then
    echo "FAIL .env.example contains non-placeholder sensitive values"
    echo "$bad_keys" | sed 's/^/  /'
    status=1
  fi
}

check_tracked_pattern "token/private-key" "$secret_pattern"
check_tracked_pattern "caddy password hash" "$hash_pattern"
check_tracked_pattern "credential assignment" "$assignment_pattern"
check_tracked_pattern "plain credential assignment" "$plain_assignment_pattern"
check_tracked_pattern "credential URL" "$credential_url_pattern"
check_env_example_placeholders
check_tracked_sensitive_paths
check_client_context_absent

if [ "$CHECK_LOCAL_SECRETS" = "1" ]; then
  check_local_pattern "token/private-key" "$secret_pattern"
  check_local_pattern "caddy password hash" "$hash_pattern"
  check_local_pattern "credential assignment" "$assignment_pattern"
  check_local_pattern "plain credential assignment" "$plain_assignment_pattern"
  check_local_pattern "credential URL" "$credential_url_pattern"
  check_sensitive_paths
fi

exit "$status"
