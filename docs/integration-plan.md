# Integration Plan

## OGM

Target resources:

- https://data.ogm.gov.tr
- https://map.ogm.gov.tr
- https://cbsportal.ogm.gov.tr/vatandas/
- https://wsapi.ogm.gov.tr/apigateway/

First technical task:

1. Inspect web map network requests.
2. Identify WMS, WFS, ArcGIS REST, vector tile, or other usable endpoints.
3. Document layers, supported formats, coordinate reference systems, and limits.
4. Build an isolated adapter for fetching bounded Stage 1 data.
5. Normalize fetched data into `ogm_parcels`.

Current integration path:

- The verified Layer A map services are public WMS endpoints under
  `https://wsapi.ogm.gov.tr/apigateway/`.
- WMS capabilities and PNG map responses are verified for the Stage 1 test
  area.
- `https://wsapi.ogm.gov.tr/apigateway/ESATIS_BOLME/0` is the first verified
  ArcGIS REST feature layer for bounded polygon ingest.
- `YAYIN_MESCERE_WMS` is available for Mescere attributes through
  `GetFeatureInfo`. Its SVG `GetMap` output can be used as a bounded Stage 1
  vector source while a geometry-capable Mescere REST endpoint is being checked.
- Portal search confirms that the public current `Vatandaş` web map exposes
  Mescere only through WMS. Older portal maps reveal the expected direct
  `YAYIN/MESCERE/MapServer`, but direct REST access is not stable enough for
  the Stage 1 ingest path.
- TUCBS metadata search identifies the target production Layer A services:
  `MEŞCERE HARİTASI - BÖLME` and `MEŞCERE HARİTASI - BÖLMECİK`, both owned by
  `ORMAN GENEL MÜDÜRLÜĞÜ` and published with WMS viewing services plus WFS
  download services.
- These TUCBS services require authenticated access or an official
  file/database export. Public WMS/SVG access is a fallback for proving the
  integration path, not the final national ingest source.

Endpoint research must record:

- service type;
- base URL;
- layer name and layer ID;
- geometry type;
- CRS;
- supported output formats;
- request limits;
- attribution or usage notes;
- date checked.

The adapter should return normalized records and raw attributes separately. It
must not leak provider-specific response shapes into API routes or database
migration code.

## CDSE / Sentinel-2

Target resource:

- https://dataspace.copernicus.eu

First technical task:

1. Verify credentials.
2. Request Sentinel-2 scenes for the bounded Stage 1 area.
3. Filter by date and cloud coverage.
4. Calculate NDVI on the bounded Stage 1 grid.
5. Store observations with acquisition metadata.

Current public test path:

- test area: Stage 1 region in northwestern Turkey;
- collection: Sentinel-2 L2A public COG assets through STAC;
- period: 2026-05-01 to 2026-05-28;
- cloud filter: below 40 percent;
- bands: red, nir, and SCL;
- NDVI source: local raster-zonal processing through `public_stac_cog`;
- storage target: cell-linked `sentinel_observations` with request metadata in JSONB.

Stage 1 should use a bounded geometry and a controlled date range. Successful
requests should record the acquisition date, cloud coverage, processing level,
statistics interval, quality mask metadata, and bands used for NDVI.

## Mapbox

Use Mapbox GL JS for the frontend and Mapbox token only as a runtime secret.

MVT generation will be introduced after the first PostGIS schema and test grid
are stable.

Mapbox tokens must stay in local environment variables. Public tokens may be
used by the browser at runtime, but they still should not be hardcoded in the
repository.

## Review Environment

The review server should be treated as a private review environment, not as a
public production workload.

Initial deployment order:

1. verify server access;
2. create a non-root deployment user;
3. install Docker and Compose;
4. configure firewall rules for the intended entrypoints only;
5. clone the private repository;
6. create `.env` on the server from the local template;
7. start the review stack with `infra/docker-compose.prod.yml`;
8. run Alembic migrations and the Stage 1 pipeline;
9. configure backups after the database volume path is final.

The production-style compose file keeps PostgreSQL, backend, and frontend host
ports bound to localhost by default. External review access should go through
the Caddy reverse proxy with HTTPS and Basic Auth, or through another deliberate
entrypoint such as a tunnel or firewall allowlist.

Do not paste server credentials, access keys, tokens, or provider account
details into repository files.
