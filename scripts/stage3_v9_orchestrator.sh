#!/usr/bin/env bash
set -u

if [[ "${MAP1_FULL_TURKEY_APPROVED:-}" != "YES" ]]; then
  printf "Full Turkey run blocked. Set MAP1_FULL_TURKEY_APPROVED=YES only after explicit user approval.\n" >&2
  exit 64
fi

cd /opt/map1-turkey || exit 1

LOG=/tmp/stage3-v10-full-orchestrator.log
GATE_NAME="${GATE_NAME:-stage3-v10-clean-mask-gate}"
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
)

log() {
  printf "%s | %s\n" "$(date -Is)" "$*" >> "$LOG"
}

container_status() {
  "${DOCKER[@]}" inspect -f "{{.State.Status}}|{{.State.ExitCode}}" "$1" 2>/dev/null || true
}

run_sql() {
  "${DOCKER[@]}" exec -i infra-db-1 sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F "|" -At' <<< "$1" 2>&1
}

grid_status() {
  run_sql "
SELECT
  run.status,
  run.total_tiles,
  run.processed_tiles,
  run.inserted_cells,
  count(*) FILTER (WHERE tile.status='pending') AS pending,
  count(*) FILTER (WHERE tile.status='running') AS running,
  count(*) FILTER (WHERE tile.status='failed') AS failed,
  count(*) FILTER (WHERE tile.status='completed') AS completed
FROM stage3_grid_generation_runs run
JOIN regions r ON r.id=run.region_id
LEFT JOIN stage3_grid_generation_tiles tile ON tile.run_id=run.id
WHERE r.code='$REGION'
  AND run.grid_version='$GRID'
GROUP BY run.id,run.status,run.total_tiles,run.processed_tiles,run.inserted_cells
ORDER BY run.id DESC
LIMIT 1;
"
}

start_worker() {
  local name="$1"
  "${COMPOSE[@]}" run -d --no-deps --name "$name" stage1-tools "${COMMON[@]}" --resume >> "$LOG" 2>&1
}

log "orchestrator start"
launched_grid=0

while true; do
  gate="$(container_status "$GATE_NAME")"
  disk="$(df -h / | awk 'NR==2{print $5 ",free=" $4}')"
  stats="$("${DOCKER[@]}" stats --no-stream --format '{{.Name}}={{.CPUPerc}},{{.MemUsage}}' infra-db-1 "$GATE_NAME" 2>/dev/null | tr '\n' ';')"
  log "gate=$gate disk=$disk stats=$stats"

  if [[ "$gate" == "exited|0" ]]; then
    if [[ "$launched_grid" -eq 0 ]] && ! "${DOCKER[@]}" ps -a --format '{{.Names}}' | grep -q '^stage3-v10-grid-main$'; then
      log "gate green; dry-run grid"
      if "${COMPOSE[@]}" run --rm --no-deps stage1-tools "${COMMON[@]}" --dry-run >> "$LOG" 2>&1; then
        log "dry-run green; launching grid main + workers"
        "${COMPOSE[@]}" run -d --no-deps --name stage3-v10-grid-main stage1-tools "${COMMON[@]}" >> "$LOG" 2>&1
        sleep 10
        for i in 1 2 3 4; do
          start_worker "stage3-v10-grid-worker-$i"
          sleep 3
        done
        launched_grid=1
      else
        log "dry-run failed; stop"
        exit 2
      fi
    fi
  elif [[ "$gate" == exited\|* ]]; then
    log "gate failed; tail follows"
    "${DOCKER[@]}" logs --tail 120 "$GATE_NAME" >> "$LOG" 2>&1
    exit 1
  fi

  gs="$(grid_status | tail -1)"
  [[ -n "$gs" ]] && log "grid=$gs"
  if [[ "$gs" == completed\|* ]]; then
    log "grid completed"
    exit 0
  fi
  if [[ "$gs" == failed\|* ]]; then
    log "grid failed"
    exit 3
  fi

  if [[ "$launched_grid" -eq 1 ]]; then
    running="$("${DOCKER[@]}" ps --format '{{.Names}}' | grep -E '^stage3-v10-grid-(main|worker)' | wc -l | tr -d ' ')"
    if [[ "$running" -lt 4 ]] && [[ "$gs" != completed\|* ]] && [[ "$gs" != failed\|* ]]; then
      stamp="$(date +%H%M%S)"
      log "running_workers_low=$running; launching replacement"
      start_worker "stage3-v10-grid-worker-repl-$stamp"
    fi
  fi

  sleep 60
done
