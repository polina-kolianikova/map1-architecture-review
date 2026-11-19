# Stage 6 Handover

Stage 6 keeps the live product closed to public access, keeps English as the
working interface language, and leaves a clean path for future language packs.

## Access

The public review domain must run through Caddy with Basic Auth enabled. Direct
frontend, backend, PostgreSQL, and Redis ports stay bound to localhost or the
Docker network. Keep backend Basic Auth empty for this Caddy-only setup. If the
backend is ever exposed outside the trusted reverse-proxy boundary, set
`BACKEND_BASIC_AUTH_USER`, `BACKEND_BASIC_AUTH_PASSWORD`, and the matching
`CADDY_BACKEND_AUTH_HEADER` used for internal API proxy requests.

This Basic Auth review entrypoint is the Stage 6 simple login/password path for
the current MVP.

Health checks:

```bash
curl -i https://<APP_HOST>/
curl -i https://<APP_HOST>/api/health
```

Unauthenticated review requests should return `401`. Authenticated requests
should load the dashboard and the API health response.

## Language Hook

The current interface copy stays in English. Future language packs can be added
without changing the GIS, Layer B, or carbon calculation logic.

Frontend extension points:

- `frontend/src/i18n/messages.js` contains the message catalog and locale
  helpers.
- `VITE_APP_LOCALE` selects the active locale.
- `VITE_APP_AVAILABLE_LOCALES` lists the locales enabled for the current
  runtime.
- `window.__MAP1_CONFIG__.locale` and `window.__MAP1_CONFIG__.availableLocales`
  can override the same values at runtime.

Backend extension points:

- `APP_DEFAULT_LOCALE` defines the default locale exposed by the API manifest.
- `APP_SUPPORTED_LOCALES` lists locales the API manifest can advertise for
  future localized responses.
- `GET /meta/localization` returns the public localization manifest.

The localization manifest is non-secret, but in production it must still be
served only through the same protected review entrypoint as the rest of the app.

To add another language later, add the locale catalog, include it in the enabled
locale list, build the frontend, and keep the same API contract.

## Operations

Before server updates, create a database backup:

```bash
scripts/backup-db.sh
```

After deployment, run the smoke checks through the review entrypoint:

```bash
API_BASE_URL=https://<APP_HOST>/api \
FRONTEND_URL=https://<APP_HOST> \
FRONTEND_AUTH="$MAP1_REVIEW_AUTH" \
BROWSER_DOCKER=1 \
scripts/check-stage1.sh
```

For frontend-only changes, also run:

```bash
npm --prefix frontend run build
```

For the Stage 6 review boundary and browser workflow, run:

```bash
FRONTEND_URL=https://<APP_HOST> \
FRONTEND_AUTH="$MAP1_REVIEW_AUTH" \
EXPECT_BASIC_AUTH=1 \
SCREENSHOT_DIR=/tmp/map1-stage6-live-screenshots \
scripts/check-stage6-live.sh
```

## User Guide

1. Open the protected review domain and enter the issued review username and
   password.
2. Use the main dashboard to choose the working scope and review portfolio-level
   cells, Layer A records, NDVI coverage, verification rows, and carbon totals.
3. Open the Turkey workspace to inspect the map, rollout totals, satellite
   signal, registry sources, asset status, and evidence log.
4. Use map focus for spatial review. Click a visible cell or a row in Asset
   Register to open the Asset Passport.
5. In Asset Passport, review asset identity, NDVI history, registry evidence,
   Total Potential, Net Annual Increment, confidence, annual deltas, and audit
   events.
6. Use the Asset Register filters for evidence, signal, carbon status, observed
   date range, or cell search. Clear filters before returning to the full
   workspace view.

## Ownership

The software rights act and depositary package are a final-step paperwork item
after the work is fully finished. Keep private credentials, tokens, and access
notes outside the repository before preparing those materials.
