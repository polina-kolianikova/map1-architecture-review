#!/usr/bin/env sh
set -eu

REGION_CODE="${STAGE4_REGION_CODE:-TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10}"
GRID_VERSION="${STAGE4_GRID_VERSION:-stage3-public-ogm-osm-union-clean-v10}"

cd "$(dirname "$0")/../backend"

python -m app.commands.check_stage4_automation \
  --region-code "$REGION_CODE" \
  --grid-version "$GRID_VERSION" \
  --detect-stuck \
  --check-provider-permissions \
  --fail-on-blockers
