# Stage 4 Layer B Automation

Stage 4 builds the automated Layer B foundation on top of the accepted Stage 3
V10 forest grid.

## Contract Scope

- Regular NDVI refreshes every 5-10 days.
- Regular biomass refreshes every 5-10 days.
- Full Turkish forest scope coverage.
- Background queue with monitoring, failure logging, and alerts.
- Reliable historical satellite data storage for trend analysis.
- `area_coefficient` stored for every grid cell so Stage 5 can apply exact
  area-weighted carbon formulas.

## Data Model

`grid_cells.area_coefficient` stores the exact cell area coefficient. For the
current 1 ha nominal grid, the coefficient is the clipped `area_ha` value
rounded to six decimal places. New and updated grid cells receive the value
from a database trigger. Existing rows must be physically backfilled before full
Layer B automation is enabled, so Stage 5 formulas can rely on the stored
coefficient without fallback ambiguity. Run the backfill in controlled batches
when there is enough database maintenance headroom:

```bash
python -m app.commands.backfill_stage4_area_coefficients \
  --table grid_cells \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --batch-size 100000
```

If the partitioned grid shadow is active for review reads, backfill it as a
separate controlled step:

```bash
python -m app.commands.backfill_stage4_area_coefficients \
  --table grid_cells_partitioned \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --batch-size 100000
```

Stage 4 stores run state in `stage4_layer_b_runs`, batch state in
`stage4_layer_b_batches`, event and failure logs in `stage4_layer_b_events`,
metric history in `stage4_layer_b_metrics`, satellite source references in
`stage4_satellite_artifacts`, worker heartbeats in `stage4_process_heartbeats`,
and alerts in `stage4_alerts`.

Heavy raw imagery is not stored in Postgres. The database stores provenance,
catalogue references, quality metadata, metric history, and optional object
storage URIs. This keeps the production database stable while preserving a
reliable trend audit trail.

## Queue

Stage 4 uses Celery with Redis. The scheduler is off by default:

```env
STAGE4_SCHEDULER_ENABLED=false
```

This prevents accidental full-scope satellite processing after deploy. To start
the worker stack locally:

```bash
docker compose -f infra/docker-compose.yml --profile stage4 up stage4-worker stage4-beat redis
```

Production compose includes `redis`, `stage4-worker`, and `stage4-beat`. The
beat process checks daily whether the active scope is due for a new run. The
cadence must remain inside the contract window:

```env
STAGE4_UPDATE_CADENCE_DAYS=10
```

The default Stage 4 target is the accepted V10 full public scope:

```env
STAGE4_REGION_CODE=TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10
STAGE4_GRID_VERSION=stage3-public-ogm-osm-union-clean-v10
```

The active implementation follows the client-approved public stack. The default
route is `public_raster_zonal` with the `public_stac_cog` source adapter. It
uses the Stage 3 completed spatial tile queue, reads public Sentinel-2 L2A COG
assets through STAC, builds the NDVI raster locally from `red`, `nir`, and
`scl`, calculates zonal statistics locally, and imports the resulting per-cell
NDVI rows into the same Stage 4 observation and metric history tables. It does
not use Sentinel Hub Process API, Sentinel Hub Batch API, CloudFerro S3, or
commercial object-storage exchange:

```env
STAGE4_PROCESSING_PATH=public
STAGE4_PROVIDER_ADAPTER=public_raster_zonal
STAGE4_NDVI_PROCESSING_MODE=public_raster_zonal
STAGE4_BATCH_SIZE=500000
STAGE4_PUBLIC_RASTER_ZONAL_MAX_CELLS_PER_BATCH=50000
STAGE4_PUBLIC_RASTER_ZONAL_RESOLUTION_M=10
STAGE4_PUBLIC_RASTER_ZONAL_MAX_PIXELS=25000000
STAGE4_PUBLIC_RASTER_ZONAL_MIN_FREE_GB=5
STAGE4_PUBLIC_RASTER_ZONAL_REUSE_EXISTING_CSV=false
STAGE4_PUBLIC_RASTER_ZONAL_RESULTS_DIR=/app/artifacts/stage4/public-raster-zonal
STAGE4_PUBLIC_SOURCE_ADAPTER=public_stac_cog
STAGE4_PUBLIC_RASTER_CACHE_ENABLED=true
STAGE4_PUBLIC_RASTER_CACHE_DIR=/app/artifacts/stage4/public-raster-cache
STAGE4_PUBLIC_STAC_API_URL=https://earth-search.aws.element84.com/v1/search
STAGE4_PUBLIC_STAC_COLLECTION=sentinel-2-l2a
STAGE4_PUBLIC_STAC_SCENE_LIMIT=24
```

The `cdse_public_statistics` adapter is diagnostic-only. The readiness check
keeps it blocked for full Turkish scope even if diagnostic limits are
accidentally raised. Use it only for bounded legacy probes, not for the current
client-approved full route.

`cdse_process` remains a legacy diagnostic source adapter only. Full-scope
public Stage 4 runs are blocked when that source is configured, because it
depends on the Sentinel Hub Process endpoint.

The worker writes raster-zonal CSV files under
`STAGE4_PUBLIC_RASTER_ZONAL_RESULTS_DIR/<run_uuid>/batch-00001.csv`. If a CSV is
already present and `STAGE4_PUBLIC_RASTER_ZONAL_REUSE_EXISTING_CSV=true`, the
worker imports it. The default path regenerates the CSV from the public NDVI
raster for the current run to avoid stale inputs. Each row includes `cell_id`,
`metric_date`, `ndvi`, `source_status`, `valid_pixel_ratio`,
`source_product_id`, `sample_count`, and `valid_pixel_count`. `ok` rows must
include a valid NDVI value; non-`ok` rows are kept as explicit skipped
public-data coverage records.

The optional prepare command exports per-batch cell GeoPackages and manifests for
manual inspection. It is not required for the queued local processor:

```bash
python -m app.commands.prepare_stage4_public_raster_zonal_inputs \
  --run-uuid <stage4-run-uuid> \
  --limit 1
```

The industrial Sentinel Hub Batch Statistical/S3 path remains available behind
the adapter layer for future scaling. It is not part of the active public-stack
Stage 4 decision. If the client later approves that route, switch both the path
and adapter explicitly and provide object-storage roots:

```env
STAGE4_PROCESSING_PATH=industrial
STAGE4_PROVIDER_ADAPTER=industrial_batch_statistical
STAGE4_NDVI_PROCESSING_MODE=batch_statistical
STAGE4_BATCH_STATISTICAL_MAX_FEATURES=700000
STAGE4_BATCH_STATISTICAL_INPUT_URI=s3://bucket/map1-turkey/stage4/input
STAGE4_BATCH_STATISTICAL_OUTPUT_URI=s3://bucket/map1-turkey/stage4/output
STAGE4_BATCH_STATISTICAL_S3_IAM_ROLE_ARN=
STAGE4_BATCH_STATISTICAL_S3_ACCESS_KEY=change-me
STAGE4_BATCH_STATISTICAL_S3_SECRET_ACCESS_KEY=change-me
STAGE4_BATCH_STATISTICAL_S3_REGION=WAW3-1
STAGE4_BATCH_STATISTICAL_S3_ENDPOINT_URL=https://s3.waw3-1.cloudferro.com
```

Prepare provider input files in chunks before uploading them to the configured
object-storage input root:

```bash
python -m app.commands.prepare_stage4_batch_statistical_inputs \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --output-dir /tmp/map1-stage4-batch-statistical-inputs \
  --batch-size 500000
```

After a provider batch finishes, import the returned JSON files into Stage 4
metrics:

```bash
python -m app.commands.import_stage4_batch_statistical_results \
  --batch-id 1 \
  --input-dir /tmp/map1-stage4-batch-statistical-output/batch-00001
```

## Manual Run

A bounded public proof dry-run is the green gate for the current client
decision:

```bash
python -m app.commands.run_stage4_public_proof \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --max-cells 10
```

Create a public proof run without processing tasks:

```bash
python -m app.commands.run_stage4_public_proof \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --max-cells 10 \
  --apply
```

Create and process the public proof inline:

```bash
python -m app.commands.run_stage4_public_proof \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --max-cells 10 \
  --apply \
  --process-inline
```

Strict proof runs require every public NDVI cell to return `ok`. Scaling probes
can use `--min-ndvi-ok-ratio` so unavailable public pixels are reported as
coverage instead of being confused with processing failures.

Check the observed public-statistics throughput before any wider run:

```bash
python -m app.commands.check_stage4_public_scaling \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --fail-if-unfit
```

The project script wraps the same proof flow. By default it runs the plan only;
set `STAGE4_PUBLIC_PROOF_APPLY=true` to execute the proof inline:

```bash
scripts/check-stage4-public-proof.sh
```

The full-scope readiness gate remains intentionally red while the active adapter
is the diagnostic public statistics path or while any physical
`area_coefficient` backfill is missing:

```bash
python -m app.commands.check_stage4_automation \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --detect-stuck \
  --check-provider-permissions \
  --fail-on-blockers
```

## Biomass

The biomass pipeline is present, but it does not invent production biomass
values. Until a biomass source or calibrated formula is configured, biomass
metrics are stored with `pending_formula`.

When a formula is approved, enable it explicitly:

```env
STAGE4_BIOMASS_FORMULA_ENABLED=true
STAGE4_BIOMASS_FORMULA_VERSION=approved-version
STAGE4_BIOMASS_NDVI_INTERCEPT=0
STAGE4_BIOMASS_NDVI_SLOPE=0
```

## Monitoring

Read-only API:

- `GET /stage4/status`
- `GET /stage4/runs`
- `GET /stage4/runs/{run_uuid}`
- `GET /stage4/alerts`
- `GET /stage4/assets/{cell_uuid}/metrics`

Full-scope CLI readiness check:

```bash
python -m app.commands.check_stage4_automation \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --detect-stuck \
  --check-provider-permissions \
  --fail-on-blockers
```

Alerts are written when a batch fails, finishes with partial errors, or loses
heartbeat beyond `STAGE4_STUCK_AFTER_MINUTES`.

Stage 4 NDVI observations use the `stage4_layer_b_ndvi` pipeline name. Stage 2
Layer B reads prefer this signal over the legacy Stage 1 NDVI signal. When the
Stage 3 partitioned evidence table exists, Stage 4 mirrors its Sentinel rows
into `sentinel_observations_partitioned` so the production read path sees the
new automation results.
