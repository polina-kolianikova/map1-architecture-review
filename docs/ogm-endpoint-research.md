# OGM Endpoint Research

Date: 2026-05-25

This note records endpoint research without storing credentials or downloaded
source data in Git.

## Known Project Targets

- `https://data.ogm.gov.tr`
- `https://map.ogm.gov.tr`
- `https://www.ogm.gov.tr`
- `https://tucbs-public-api.csb.gov.tr/ormansu_orman`

## Verified Findings

- `https://www.ogm.gov.tr` is reachable from the development environment.
- The official E-Harita page redirects to `https://cbsportal.ogm.gov.tr/vatandas/`.
- `cbsportal.ogm.gov.tr` resolves, but requests to the citizen map page and
  common ArcGIS REST paths timed out from the local development environment on
  2026-05-28.
- `map.ogm.gov.tr` and `data.ogm.gov.tr` did not resolve during the first
  development-environment check.
- `cbs.ogm.gov.tr` resolves locally, but a request to
  `/arcgis/rest/services?f=pjson` timed out during the first check.
- A public geospatial catalog entry lists a candidate OGM ArcGIS Server at
  `https://orbiscbs.ogm.gov.tr/arcgis/rest/services`, but the hostname did not
  resolve from the local WSL environment during the first check.
- Turkish GIS documentation and GIS software help pages list
  `https://tucbs-public-api.csb.gov.tr/ormansu_orman` as an official/public WMS
  forest service candidate. Requests to this endpoint timed out during the
  first connectivity checks on 2026-05-28.
- `https://cbsportal.ogm.gov.tr/vatandas/` returns the public E-Harita
  application from the project network path used for endpoint verification.
- The E-Harita web map item is
  `https://cbsportal.ogm.gov.tr/ogmportal/sharing/rest/content/items/1d210d5cd6cd42ecb7431e28995a0bc8`.
- Portal search by the Turkish term `meşcere` returns the current `Vatandaş`
  web map and two older web maps: `Vatandaş_test` and `Yorum`.
- The web map exposes five public OGM WMS layers through
  `https://wsapi.ogm.gov.tr/apigateway/...`.
- `ARAZI_ORTUSU_WMS` returned WMS capabilities and a PNG GetMap response for
  the Stage 1 test area.
- The older `Vatandaş_test` web map references direct ArcGIS MapServer URLs,
  including `https://cbs.ogm.gov.tr/arcgis/rest/services/YAYIN/MESCERE/MapServer`.
  Direct REST requests to this service timed out during the check.
- The current public `Vatandaş` web map does not expose Mescere as ArcGIS REST.
  It exposes the public WMS layers only.
- The `Yorum` web map also references the same direct Mescere MapServer, but
  the direct REST path timed out in the same way.
- The OGM gateway does not expose a public REST service under tested names such
  as `YAYIN_MESCERE`, `MESCERE`, `YAYIN_BOLMECIK`, `BOLMECIK`,
  `ESATIS_MESCERE`, or `ESATIS_BOLMECIK`.
- `https://wsapi.ogm.gov.tr/apigateway/YAYIN_MESCERE_WMS` is reachable and
  supports WMS `GetFeatureInfo` in XML, HTML, plain text, and GeoJSON formats.
  The GeoJSON `GetFeatureInfo` response includes stand attributes but does not
  include polygon geometry.
- The same `YAYIN_MESCERE_WMS` service supports `GetMap` with
  `image/svg+xml`. For the Stage 1 test area, SVG output returned nine
  vector paths for the Mescere/Bolmecik layer. Sampling each path with
  `GetFeatureInfo` returned `OBJECTID`, `MESCERE`, `ISLEM`, and `RENK_KODU`
  attributes.
- `https://wsapi.ogm.gov.tr/apigateway/ESATIS_BOLME/0` is a working ArcGIS REST
  feature layer with polygon geometry. A bounded query over the Stage 1 test area
  returned three OGM compartment polygons with attributes and rings in EPSG:4326.
- The `E-Satış Harita Ekranı` web map uses
  `https://wsapi.ogm.gov.tr/apigateway/ESATIS_BOLME`, which confirms that
  `ESATIS_BOLME/0` is an official public OGM gateway layer rather than a guessed
  endpoint.
- Public documentation for the TUCBS base data model lists `Mescre haritası`
  with `Bölme-Bölmecik` area geometry at 1:25,000 scale and attributes such as
  plan year, compartment number, stand type, stand code, crown closure, age
  class, slope, ownership, and color code. This matches the data shape needed
  for Layer A, but the public model document is not itself a data service.
- The TUCBS public metadata platform lists the exact official OGM layers needed
  for Layer A:
  - `MEŞCERE HARİTASI - BÖLME`
  - `MEŞCERE HARİTASI - BÖLMECİK`
- Both layers are owned by `ORMAN GENEL MÜDÜRLÜĞÜ`, use the TUCBS layer
  `MevcutAraziKullanimiNesnesi`, and have one viewing service and one download
  service each.
- The published metadata records expose WMS and WFS service IDs:
  - `MEŞCERE HARİTASI - BÖLME`: metadata
    `e4b99c3e-52f9-4855-8603-30b29d65fd6d`, WMS
    `45286092-383a-47b0-8da2-3de69726be48`, WFS
    `ccb2a975-bff6-4a90-8fb4-07ec6316daa5`
  - `MEŞCERE HARİTASI - BÖLMECİK`: metadata
    `204ebee5-96b1-4d9f-9caf-f9495d81a3cc`, WMS
    `d9c70f7a-891a-449b-b4ab-544fa3fbc434`, WFS
    `20f1f6da-5185-42d6-b4dc-7e88b547ad9d`
- These TUCBS records are restricted services. They appear in the public
  metadata search, but not in the open-data service list. The production access
  route is authenticated TUCBS/OGM service access or an official export supplied
  by the client.
- The normal Mescere source expected for long-term Layer A ingestion is still an
  authenticated TUCBS/OGM WFS download service or an official file/database
  export. The public WMS/SVG path is suitable for a bounded Stage 1 proof of
  access, but it should not be treated as the final production source for
  national-scale ingestion.

## Verified OGM WMS Services

- `https://wsapi.ogm.gov.tr/apigateway/ARAZI_ORTUSU_WMS` - Arazi Ortusu
- `https://wsapi.ogm.gov.tr/apigateway/YAYIN_SEFLIK_WMS` - Isletme Seflikleri
- `https://wsapi.ogm.gov.tr/apigateway/YAYIN_ISLETME_WMS` - Isletme Mudurlukleri
- `https://wsapi.ogm.gov.tr/apigateway/INT_BOLGE_WMS` - Bolge Mudurlugu
- `https://wsapi.ogm.gov.tr/apigateway/YAYIN_INDEKS_WMS` - Pafta Indeksi
- `https://wsapi.ogm.gov.tr/apigateway/YAYIN_MESCERE_WMS` - Mescere
  attributes through WMS GetFeatureInfo and bounded SVG vector output

## Verified OGM Feature Services

- `https://wsapi.ogm.gov.tr/apigateway/ESATIS_BOLME/0` - OGM compartment
  polygons through ArcGIS REST query

## Candidate URLs To Verify

- `https://cbs.ogm.gov.tr/arcgis/rest/services?f=pjson`
- `https://cbsportal.ogm.gov.tr/arcgis/rest/services`
- `https://cbsportal.ogm.gov.tr/server/rest/services`
- `https://cbsportal.ogm.gov.tr/portal/sharing/rest`
- `https://orbiscbs.ogm.gov.tr/arcgis/rest/services?f=pjson`
- `https://orbiscbs.ogm.gov.tr/arcgis/rest/info?f=pjson`
- `https://tucbs-public-api.csb.gov.tr/ormansu_orman?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0`

## Next Checks

1. Use `ESATIS_BOLME/0/query` as the first bounded Layer A polygon ingest path.
2. Keep the Layer A adapter boundary ready for the two OGM Mescere WFS download
   services listed above. If authenticated service access is not available, ask
   for an official export in Shapefile, GeoPackage, GeoJSON, FileGDB, or PostGIS
   dump format.
3. Until authenticated WFS access or an official export is available, use the
   bounded SVG plus GetFeatureInfo path only for the bounded Stage 1 Mescere
   review area.
4. Do not store cookies, tokens, access keys, or downloaded production datasets
   in the repository.
