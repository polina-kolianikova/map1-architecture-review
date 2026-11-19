#!/usr/bin/env sh
set -eu

FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
FRONTEND_AUTH="${FRONTEND_AUTH:-}"
EXPECT_MAPBOX_READY="${EXPECT_MAPBOX_READY:-1}"
EXPECT_VECTOR_TILES="${EXPECT_VECTOR_TILES:-auto}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-}"
BROWSER_DOCKER="${BROWSER_DOCKER:-0}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.61.0-noble@sha256:111dde95859f2c659291cb60e698f9048a8fc30b35b4ddb7c90f9cb5b73062d9}"

if [ "${PLAYWRIGHT_INSTALL:-0}" = "1" ]; then
  (
    cd frontend
    npx playwright install chromium
  )
fi

echo "Checking browser review interface at $FRONTEND_URL"
if [ "$BROWSER_DOCKER" = "1" ]; then
  work_dir="$(mktemp -d)"
  env_file="$(mktemp)"
  trap 'rm -rf "$work_dir" "$env_file"' EXIT
  screenshot_dir="${SCREENSHOT_DIR:-$work_dir/screenshots}"
  mkdir -p "$screenshot_dir"
  {
    printf 'FRONTEND_URL=%s\n' "$FRONTEND_URL"
    printf 'FRONTEND_AUTH=%s\n' "$FRONTEND_AUTH"
    printf 'EXPECT_MAPBOX_READY=%s\n' "$EXPECT_MAPBOX_READY"
    printf 'EXPECT_VECTOR_TILES=%s\n' "$EXPECT_VECTOR_TILES"
    printf 'SCREENSHOT_DIR=/screenshots\n'
  } > "$env_file"
  $DOCKER_BIN run --rm \
    --network host \
    --user "$(id -u):$(id -g)" \
    --env-file "$env_file" \
    -v "$(pwd)/frontend:/src:ro" \
    -v "$work_dir:/work" \
    -v "$screenshot_dir:/screenshots" \
    -w /work \
    "$PLAYWRIGHT_IMAGE" \
    sh -lc "cp -a /src/. /work/ && npm ci --no-audit --no-fund && npm run test:stage1-browser"
  exit 0
fi

(
  cd frontend
  FRONTEND_URL="$FRONTEND_URL" \
  FRONTEND_AUTH="$FRONTEND_AUTH" \
  EXPECT_MAPBOX_READY="$EXPECT_MAPBOX_READY" \
  EXPECT_VECTOR_TILES="$EXPECT_VECTOR_TILES" \
  SCREENSHOT_DIR="$SCREENSHOT_DIR" \
  npm run test:stage1-browser
)
