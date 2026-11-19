#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../backend"

python3 -m app.commands.prepare_stage4_public_raster_zonal_inputs "$@"
