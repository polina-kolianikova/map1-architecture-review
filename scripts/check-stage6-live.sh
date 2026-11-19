#!/usr/bin/env sh
set -eu

FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
FRONTEND_AUTH="${FRONTEND_AUTH:-}"
EXPECT_BASIC_AUTH="${EXPECT_BASIC_AUTH:-0}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-}"
PLAYWRIGHT_OUTPUT_DIR="${PLAYWRIGHT_OUTPUT_DIR:-/tmp/map1-stage6-live-results}"
PLAYWRIGHT_LAST_RUN_OUTPUT_FILE="${PLAYWRIGHT_LAST_RUN_OUTPUT_FILE:-/tmp/map1-stage6-live-last-run.json}"

export FRONTEND_URL FRONTEND_AUTH EXPECT_BASIC_AUTH SCREENSHOT_DIR
export PLAYWRIGHT_OUTPUT_DIR PLAYWRIGHT_LAST_RUN_OUTPUT_FILE

auth_state="unset"
if [ -n "$FRONTEND_AUTH" ]; then
  auth_state="set"
fi
printf 'Stage 6 live check: FRONTEND_URL=%s EXPECT_BASIC_AUTH=%s FRONTEND_AUTH=%s\n' \
  "$FRONTEND_URL" "$EXPECT_BASIC_AUTH" "$auth_state"

cd "$(dirname "$0")/../frontend"
exec npm run test:stage6-live
