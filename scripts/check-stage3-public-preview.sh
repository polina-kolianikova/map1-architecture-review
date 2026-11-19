#!/usr/bin/env sh
set -eu

FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
FRONTEND_AUTH="${FRONTEND_AUTH:-}"
EXPECT_PUBLIC_PREVIEW="${EXPECT_PUBLIC_PREVIEW:-1}"
EXPECT_VECTOR_TILES="${EXPECT_VECTOR_TILES:-1}"
BROWSER_DOCKER="${BROWSER_DOCKER:-0}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.61.0-noble@sha256:111dde95859f2c659291cb60e698f9048a8fc30b35b4ddb7c90f9cb5b73062d9}"

if [ "${PLAYWRIGHT_INSTALL:-0}" = "1" ]; then
  (
    cd frontend
    npx playwright install chromium
  )
fi

echo "Checking Stage 3 public preview at $FRONTEND_URL"
if [ "$BROWSER_DOCKER" = "1" ]; then
  work_dir="$(mktemp -d)"
  env_file="$(mktemp)"
  screenshot_dir="${SCREENSHOT_DIR:-$work_dir/screenshots}"
  trap 'rm -rf "$work_dir" "$env_file"' EXIT
  mkdir -p "$screenshot_dir"
  {
    printf 'FRONTEND_URL=%s\n' "$FRONTEND_URL"
    printf 'FRONTEND_AUTH=%s\n' "$FRONTEND_AUTH"
    printf 'EXPECT_PUBLIC_PREVIEW=%s\n' "$EXPECT_PUBLIC_PREVIEW"
    printf 'EXPECT_VECTOR_TILES=%s\n' "$EXPECT_VECTOR_TILES"
    if [ -n "${STAGE3_QA_REGION_CODE:-}" ]; then
      printf 'STAGE3_QA_REGION_CODE=%s\n' "$STAGE3_QA_REGION_CODE"
    fi
    if [ -n "${STAGE3_QA_GRID_VERSION:-}" ]; then
      printf 'STAGE3_QA_GRID_VERSION=%s\n' "$STAGE3_QA_GRID_VERSION"
    fi
    if [ -n "${STAGE3_QA_SCOPE_LABEL:-}" ]; then
      printf 'STAGE3_QA_SCOPE_LABEL=%s\n' "$STAGE3_QA_SCOPE_LABEL"
    fi
    if [ -n "${STAGE3_QA_SCOPE_BADGE:-}" ]; then
      printf 'STAGE3_QA_SCOPE_BADGE=%s\n' "$STAGE3_QA_SCOPE_BADGE"
    fi
    if [ -n "${STAGE3_QA_CHECKPOINTS:-}" ]; then
      printf 'STAGE3_QA_CHECKPOINTS=%s\n' "$STAGE3_QA_CHECKPOINTS"
    fi
    if [ -n "${STAGE3_QA_EMPTY_CHECKPOINTS:-}" ]; then
      printf 'STAGE3_QA_EMPTY_CHECKPOINTS=%s\n' "$STAGE3_QA_EMPTY_CHECKPOINTS"
    fi
  } > "$env_file"
  $DOCKER_BIN run --rm \
    --network host \
    --user "$(id -u):$(id -g)" \
    --env-file "$env_file" \
    -v "$(pwd)/frontend:/src:ro" \
    -v "$work_dir:/work" \
    -v "$screenshot_dir:/work/test-results" \
    -w /work \
    "$PLAYWRIGHT_IMAGE" \
    sh -lc "cp -a /src/. /work/ && npm ci --no-audit --no-fund && npm run test:stage3-public-preview"
  exit 0
fi

(
  cd frontend
  FRONTEND_URL="$FRONTEND_URL" \
  FRONTEND_AUTH="$FRONTEND_AUTH" \
  EXPECT_PUBLIC_PREVIEW="$EXPECT_PUBLIC_PREVIEW" \
  EXPECT_VECTOR_TILES="$EXPECT_VECTOR_TILES" \
  STAGE3_QA_REGION_CODE="${STAGE3_QA_REGION_CODE:-}" \
  STAGE3_QA_GRID_VERSION="${STAGE3_QA_GRID_VERSION:-}" \
  STAGE3_QA_SCOPE_LABEL="${STAGE3_QA_SCOPE_LABEL:-}" \
  STAGE3_QA_SCOPE_BADGE="${STAGE3_QA_SCOPE_BADGE:-}" \
  STAGE3_QA_CHECKPOINTS="${STAGE3_QA_CHECKPOINTS:-}" \
  STAGE3_QA_EMPTY_CHECKPOINTS="${STAGE3_QA_EMPTY_CHECKPOINTS:-}" \
  npm run test:stage3-public-preview
)
