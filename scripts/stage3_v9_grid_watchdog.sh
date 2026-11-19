#!/usr/bin/env bash
set -u

if [[ "${MAP1_FULL_TURKEY_APPROVED:-}" != "YES" ]]; then
  printf "Full Turkey watchdog blocked. Set MAP1_FULL_TURKEY_APPROVED=YES only after explicit user approval.\n" >&2
  exit 64
fi

cd /opt/map1-turkey || exit 1

LOG=/tmp/stage3-v10-grid-watchdog.log
DOCKER=(sudo -n /usr/bin/docker)
COMPOSE=(
  sudo -n /usr/bin/docker compose
  --env-file "${MAP1_ENV_FILE:-/etc/map1-turkey/map1.env}"
  -f infra/docker-compose.yml
  -f infra/docker-compose.prod.yml
  --profile tools
)
REGION="TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10"
GRID="stage3-public-ogm-osm-union-clean-v10"
DESIRED="${STAGE3_GRID_DESIRED_WORKERS:-5}"

COMMON=(
  python -m app.commands.run_stage3_grid_generation_batched
  --region-code "$REGION"
  --grid-version "$GRID"
  --grid-size-m 100
  --projected-srid 6933
  --tile-size-m 10000
  --max-failed-tiles 0
  --use-region-mask-components
  --min-mask-component-area-ha 1.0
  --ack-full-turkey-user-approved
  --resume
)

log() {
  printf "%s | %s\n" "$(date -Is)" "$*" >> "$LOG"
}

run_sql() {
  "${DOCKER[@]}" exec -i infra-db-1 sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F "|" -At' <<< "$1" 2>/dev/null
}

start_worker() {
  local name="stage3-v10-grid-worker-auto-$(date +%H%M%S)-$RANDOM"
  log "start_worker=$name"
  "${COMPOSE[@]}" run -d --no-deps --name "$name" stage1-tools "${COMMON[@]}" >> "$LOG" 2>&1
}

log "watchdog start desired=$DESIRED"

while true; do
  status_line="$(run_sql "
SELECT
  run.status,
  run.total_tiles,
  run.processed_tiles,
  run.inserted_cells,
  count(*) FILTER (WHERE tile.status='pending') AS pending,
  count(*) FILTER (WHERE tile.status='running') AS running_tiles,
  count(*) FILTER (WHERE tile.status='failed') AS failed_tiles,
  count(*) FILTER (WHERE tile.status='completed') AS completed_tiles
FROM stage3_grid_generation_runs run
JOIN regions r ON r.id=run.region_id
LEFT JOIN stage3_grid_generation_tiles tile ON tile.run_id=run.id
WHERE r.code='$REGION'
  AND run.grid_version='$GRID'
GROUP BY run.id,run.status,run.total_tiles,run.processed_tiles,run.inserted_cells
ORDER BY run.id DESC
LIMIT 1;
")"
  active="$("${DOCKER[@]}" ps --format '{{.Names}}' | grep -E '^stage3-v10-grid-(main|worker)' | wc -l | tr -d ' ')"
  disk="$(df -h / | awk 'NR==2{print $5 ",free=" $4}')"
  log "status=$status_line active_containers=$active disk=$disk"

  IFS='|' read -r run_status total processed inserted pending running_tiles failed_tiles completed_tiles <<< "$status_line"
  if [[ "$run_status" == "completed" ]]; then
    log "completed"
    exit 0
  fi
  if [[ "$run_status" == "failed" ]]; then
    log "failed"
    exit 1
  fi
  if [[ "$run_status" == "running" && "${pending:-0}" -gt 0 && "$active" -lt "$DESIRED" ]]; then
    start_worker
  fi

  sleep 60
done
