#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../backend"

REGION_CODE="${STAGE4_REGION_CODE:-TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10}"
GRID_VERSION="${STAGE4_GRID_VERSION:-stage3-public-ogm-osm-union-clean-v10}"

python3 -m app.commands.check_stage4_public_scaling \
  --region-code "$REGION_CODE" \
  --grid-version "$GRID_VERSION" \
  "$@"
