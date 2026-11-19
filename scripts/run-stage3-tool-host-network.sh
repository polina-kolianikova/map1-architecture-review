#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MAP1_ENV_FILE:-$ROOT_DIR/.env}"
IMAGE="${MAP1_STAGE3_TOOLS_IMAGE:-infra-stage1-tools:latest}"
OGM_PROXY="${MAP1_OGM_PROXY:-}"
ALLOW_DIRECT_EGRESS="${MAP1_ALLOW_DIRECT_EGRESS:-0}"
REPORT_DIR="${MAP1_STAGE3_REPORT_DIR:-$ROOT_DIR/stage3-reports}"
RUN_UID="$(id -u)"
RUN_GID="$(id -g)"
TEMP_DIR="$(mktemp -d)"
ENV_ALLOWLIST="$TEMP_DIR/environment"
PROXY_ENV_FILE=""
PROXY_CURL_CONFIG=""
CHILD_PID=""
RUN_NAME="map1-stage3-tool-${TEMP_DIR##*/}"
cleanup() {
  local status=$?
  trap - EXIT
  trap '' INT TERM HUP
  if [ -n "$CHILD_PID" ]; then
    # Docker creates its workload outside the shell process tree. Stop only our
    # unpredictable container name; killing the CLI alone is not sufficient.
    if ! timeout --kill-after=2s 15s "${DOCKER[@]}" stop --time 5 "$RUN_NAME" >/dev/null 2>&1; then
      echo 'Tool container stop could not be confirmed; inspect the owned run name:' "$RUN_NAME" >&2
      [ "$status" != 0 ] || status=1
    fi
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    for ((attempt=0; attempt<50; attempt++)); do
      kill -0 "$CHILD_PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
  fi
  rm -rf -- "$TEMP_DIR"
  exit "$status"
}
trap cleanup EXIT
forward_signal() {
  local name="$1" status="$2"
  trap '' INT TERM HUP
  [ -z "$CHILD_PID" ] || kill -s "$name" "$CHILD_PID" 2>/dev/null || true
  exit "$status"
}
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM
trap 'forward_signal HUP 129' HUP

run_container() {
  local status=0
  # Keep this shell alive: exec would discard its EXIT cleanup. Backgrounding
  # makes wait interruptible so signals sent to this wrapper reach Docker.
  "${DOCKER[@]}" run --name "$RUN_NAME" "$@" &
  CHILD_PID=$!
  wait "$CHILD_PID" || status=$?
  CHILD_PID=""
  return "$status"
}
command -v timeout >/dev/null || { echo 'timeout is required for bounded cleanup' >&2; exit 1; }

if [ -r "$ENV_FILE" ] && [ ! -f "$ENV_FILE" ]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$REPORT_DIR"

if [ -z "$OGM_PROXY" ] && [ "$ALLOW_DIRECT_EGRESS" != "1" ]; then
  echo "MAP1_OGM_PROXY must be set for host-network Stage 3 runs, or set MAP1_ALLOW_DIRECT_EGRESS=1 explicitly." >&2
  exit 1
fi

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo -n docker)
fi

if [ ! -r "$ENV_FILE" ]; then
  POSTGRES_PORT="${POSTGRES_HOST_PORT:-5433}"
  PROXY_ARGS=()
  if [ -n "$OGM_PROXY" ]; then
    PROXY_ENV_FILE="$TEMP_DIR/proxy.env"
    printf 'HTTP_PROXY=%s\nHTTPS_PROXY=%s\nOGM_PROXY_URL=%s\nNO_PROXY=localhost,127.0.0.1\n' \
      "$OGM_PROXY" "$OGM_PROXY" "$OGM_PROXY" > "$PROXY_ENV_FILE"
    PROXY_ARGS=(--env-file "$PROXY_ENV_FILE")
  fi

  run_container --rm --network host \
    --user "$RUN_UID:$RUN_GID" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    -v "$REPORT_DIR:/reports" \
    -e POSTGRES_HOST=127.0.0.1 \
    -e POSTGRES_PORT="$POSTGRES_PORT" \
    "${PROXY_ARGS[@]}" \
    "$IMAGE" "$@"
  exit $?
fi

env_value() {
  key="$1"
  awk -F= -v key="$key" '
    $1 == key {
      value=$0
      sub(/^[^=]*=/, "", value)
      print value
      exit
    }
  ' "$ENV_FILE"
}

awk -F= '
  $1 ~ /^(POSTGRES_DB|OGM_BOLME_FEATURE_URL|OGM_MESCERE_WMS_URL|OGM_TLS_VERIFY|OGM_PROXY_URL)$/ {
    print
  }
' "$ENV_FILE" > "$ENV_ALLOWLIST"

POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-$(env_value POSTGRES_HOST_PORT)}"
POSTGRES_PORT="${POSTGRES_HOST_PORT:-5433}"
POSTGRES_APP_USER="$(env_value POSTGRES_APP_USER)"
POSTGRES_APP_PASSWORD="$(env_value POSTGRES_APP_PASSWORD)"
if [ -z "$POSTGRES_APP_USER" ] || [ -z "$POSTGRES_APP_PASSWORD" ]; then
  echo "POSTGRES_APP_USER and POSTGRES_APP_PASSWORD must be set in $ENV_FILE" >&2
  exit 1
fi
{
  printf 'POSTGRES_USER=%s\n' "$POSTGRES_APP_USER"
  printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_APP_PASSWORD"
} >> "$ENV_ALLOWLIST"

PROXY_ARGS=()
if [ -n "$OGM_PROXY" ]; then
  PROXY_ENV_FILE="$TEMP_DIR/proxy.env"
  {
    printf 'HTTP_PROXY=%s\n' "$OGM_PROXY"
    printf 'HTTPS_PROXY=%s\n' "$OGM_PROXY"
    printf 'OGM_PROXY_URL=%s\n' "$OGM_PROXY"
    printf 'NO_PROXY=localhost,127.0.0.1\n'
  } > "$PROXY_ENV_FILE"
  if command -v curl >/dev/null 2>&1; then
    PROXY_CURL_CONFIG="$TEMP_DIR/proxy.curl"
    {
      printf 'proxy = "%s"\n' "$OGM_PROXY"
      printf 'max-time = 10\n'
      printf 'fail\n'
      printf 'silent\n'
      printf 'show-error\n'
    } > "$PROXY_CURL_CONFIG"
    curl --config "$PROXY_CURL_CONFIG" https://api.ipify.org >/dev/null
  fi
  PROXY_ARGS=(
    --env-file "$PROXY_ENV_FILE"
  )
fi

run_container --rm --network host \
  --user "$RUN_UID:$RUN_GID" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v "$REPORT_DIR:/reports" \
  --env-file "$ENV_ALLOWLIST" \
  -e POSTGRES_HOST=127.0.0.1 \
  -e POSTGRES_PORT="$POSTGRES_PORT" \
  "${PROXY_ARGS[@]}" \
  "$IMAGE" "$@"
