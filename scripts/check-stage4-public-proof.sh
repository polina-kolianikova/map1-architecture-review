#!/usr/bin/env sh
set -eu

REGION_CODE="${STAGE4_REGION_CODE:-TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10}"
GRID_VERSION="${STAGE4_GRID_VERSION:-stage3-public-ogm-osm-union-clean-v10}"
MAX_CELLS="${STAGE4_PUBLIC_PROOF_MAX_CELLS:-1}"
LOOKBACK_DAYS="${STAGE4_PUBLIC_PROOF_LOOKBACK_DAYS:-90}"
MAX_CLOUD="${STAGE4_PUBLIC_PROOF_MAX_CLOUD:-80}"

cd "$(dirname "$0")/../backend"

if [ "${STAGE4_PUBLIC_PROOF_APPLY:-false}" = "true" ]; then
  python -m app.commands.run_stage4_public_proof \
    --region-code "$REGION_CODE" \
    --grid-version "$GRID_VERSION" \
    --max-cells "$MAX_CELLS" \
    --lookback-days "$LOOKBACK_DAYS" \
    --max-cloud "$MAX_CLOUD" \
    --apply \
    --process-inline
else
  python -m app.commands.run_stage4_public_proof \
    --region-code "$REGION_CODE" \
    --grid-version "$GRID_VERSION" \
    --max-cells "$MAX_CELLS" \
    --lookback-days "$LOOKBACK_DAYS" \
    --max-cloud "$MAX_CLOUD"
fi
