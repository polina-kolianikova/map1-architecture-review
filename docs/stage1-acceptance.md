# Stage 1 Acceptance Checklist

This checklist verifies the current public-data MVP path for Stage 1.

## What Must Work

- Backend starts and returns `/health`.
- Database connection and PostGIS are available through `/health/db`.
- Stage 1 summary returns loaded grid, Layer A, Sentinel observations,
  verification results, and audit events.
- Layer A public OGM records are available through the API.
- Sentinel/CDSE NDVI observations are available through the API.
- Sentinel/CDSE observations include real non-null NDVI values from the
  Sentinel Hub Statistical API, not only unavailable/request-failed rows.
- Layer C verification results are available through the API.
- Layer C audit history is available through the API.
- Layer C results record the relationship between the current public Layer A
  polygon set and the Sentinel signal used for the decision.
- Stage 1 API data is scoped to the active region and grid version.
- At least one active public OGM REST source was checked recently.
- CDSE source failures retire stale current observations and become
  insufficient-data verification results.
- Frontend opens from the review server or local stack and shows summary,
  verification, and audit trail data.
- The review browser check uses the live Mapbox map by default. Static SVG is
  only a local fallback when no Mapbox token is configured.

## Local Check

Prepare the database and runtime roles:

```bash
docker compose --env-file .env -f infra/docker-compose.prod.yml up -d db
docker compose --env-file .env -f infra/docker-compose.prod.yml --profile tools run --rm migrate
scripts/configure-db-runtime-role.sh
```

Populate the Stage 1 data:

```bash
docker compose --env-file .env -f infra/docker-compose.prod.yml --profile tools run --rm --no-deps stage1-tools \
  python -m app.commands.run_stage1_pipeline --ogm-timeout 12
docker compose --env-file .env -f infra/docker-compose.prod.yml --profile demo up -d --build
```

Run the acceptance check:

```bash
BROWSER_DOCKER=1 scripts/check-stage1.sh
scripts/check-stage1-audit.sh
```

## Server Check

Point the same check at the review server or configured review entrypoint:

```bash
API_BASE_URL=https://review.example.com/api \
FRONTEND_URL=https://review.example.com \
FRONTEND_AUTH=review:use-the-local-review-password \
BROWSER_DOCKER=1 \
scripts/check-stage1.sh
```

The check stores review credentials only in temporary curl config files, so the
password is not passed as a command-line argument to curl.

## Expected Result

The script should print `OK` for:

- API health;
- PostGIS health;
- grid cells;
- Layer A;
- Sentinel observations;
- real Sentinel NDVI observations;
- real Sentinel NDVI coverage across the current Stage 1 grid;
- fresh Sentinel NDVI observations inside the configured observation window;
- verification results;
- verification audit events;
- frontend;
- frontend API proxy;
- browser-rendered review interface with the live Mapbox map.

The audit check should also confirm that a verification rerun writes audit
events, preserves non-current verification rows, keeps one latest verification
row per cell, keeps runtime database privileges narrow, stores source snapshots
in audit metadata, compares Layer A with
Sentinel input in verification metadata, preserves grid and audit identity
across grid reruns, rejects geometry changes under the same grid version, keeps
audit history stable when source rows are refreshed, preserves current Layer A
rows outside the target region, preserves last known current Layer A rows on
empty OGM responses, normalizes invalid source geometry, marks a successful empty
Sentinel statistics response as insufficient data, marks missing cells as
insufficient data during a partial Sentinel input, retires stale observations
when a CDSE cell request or source-level CDSE lookup fails, ignores Layer A rows
from another region, rejects boundary-only Layer A intersections, keeps the
Stage 1 API scoped to the target region and grid version, excludes rows whose
metadata does not match their grid cell scope, and marks all cells as
insufficient data when current Sentinel input is unavailable.

The check validates the bounded Stage 1 MVP path. It does not claim full
national-scale production ingestion or closed TUCBS/OGM WFS access.
