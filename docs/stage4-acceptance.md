# Stage 4 Acceptance Checklist

Stage 4 is accepted when Layer B automation is implemented and verified on the
accepted Stage 3 V10 grid without regressing Stage 3 map reads.

## Required Checks

- `area_coefficient` exists on the active grid table.
- `area_coefficient` is physically populated for every cell in the accepted V10
  scope before full Layer B automation is enabled.
- Stage 4 run, batch, event log, metric, artifact, heartbeat, and alert tables
  exist.
- A dry-run proof plan reports the client-approved public adapter, bounded cell
  count, and no dependency on Sentinel Hub Process API, commercial Batch API, or
  CloudFerro S3.
- A full-scope dry-run reports the accepted V10 scope through
  `public_raster_zonal` and stays blocked until shared cache and disk-capacity
  gates are satisfied. The legacy diagnostic public statistics adapter remains
  blocked for full Turkish scope.
- Celery worker and beat services start with Redis.
- The active NDVI path goes through the Stage 4 provider adapter and the public
  source adapter. The public statistics adapter and `cdse_process` source are
  diagnostic-only; the full public path is `public_raster_zonal` with
  `public_stac_cog`; the Batch Statistical/S3 adapter is retained only for a
  future explicit switch.
- A controlled `public_raster_zonal` proof batch reads public Sentinel-2 COG
  assets, builds NDVI locally, calculates local zonal statistics, and writes
  Stage 4 Sentinel observations and `stage4_layer_b_metrics` rows.
- Biomass batches write `pending_formula` until an approved biomass source or
  calibrated formula is configured.
- Failed or stuck batches create rows in `stage4_layer_b_events` and
  `stage4_alerts`.
- `/stage4/status`, `/stage4/runs`, `/stage4/runs/{run_uuid}`, and
  `/stage4/alerts` return read-only monitoring data.
- `/stage4/assets/{cell_uuid}/metrics` returns historical NDVI and biomass
  metrics with `area_coefficient` and area-weighted values.
- Backend tests pass.
- Frontend build and browser smoke pass in an environment with valid Node native
  dependencies and browser write permissions.

## Commands

Optional physical backfill for the active grid:

```bash
python -m app.commands.backfill_stage4_area_coefficients \
  --table grid_cells \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --batch-size 100000
```

Run the expected-red full-scope readiness gate:

```bash
scripts/check-stage4-automation.sh
```

Run the expected-green bounded public proof plan:

```bash
scripts/check-stage4-public-proof.sh
```

Plan automation:

```bash
python -m app.commands.run_stage4_public_proof \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --max-cells 10
```

Create and process the bounded public proof:

```bash
python -m app.commands.run_stage4_public_proof \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --max-cells 10 \
  --apply \
  --process-inline
```

For larger public-stack scaling probes, keep the run bounded and set an
explicit NDVI coverage gate, for example `--min-ndvi-ok-ratio 0.90`. The command
still fails on processing errors, open alerts, or missing metric rows.

Audit the observed public raster-zonal throughput before any wider run:

```bash
scripts/check-stage4-public-scaling.sh --fail-if-unfit
```

## Production Gate

Keep `STAGE4_SCHEDULER_ENABLED=false` until the first controlled backfill,
`public_raster_zonal` proof, disk-capacity check, and alert routing check pass.
Enable the scheduler only with the full-scope public adapter or a later
client-approved adapter switch.
