# Frontend

The current presentation labels, components, API clients, hooks and styles are retained. No Mapbox key, production runtime configuration or private calculation fixture is included.

Use Node 22, run `npm ci --ignore-scripts`, then `npm run dev`. The local development proxy forwards `/api` to the backend. Database-backed views require locally supplied schema and data. Missing data is shown as unavailable. Only private calculation configuration/rule endpoints are explicit 501 boundaries.

Observation color breakpoints and the area-to-coefficient fallback are removed. Carbon values are read from supplied API records; private calculations are not implemented in the browser. Presentation coordinate transforms, pagination and formatting remain.

The original compiled production bundle and mathematical browser fixtures are not copied. The complete source-path mapping and explicit private-fixture redactions are documented in the root review evidence.
