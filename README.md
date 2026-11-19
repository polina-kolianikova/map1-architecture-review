# map1-architecture-review

Map 1's source directory and file structure is retained for architecture review. Nonproprietary application, migration, container, backup and CI implementations are included. Private calculation bodies, model coefficients, risk thresholds, assessment fixtures, secrets, original Git history and live server state are excluded.

## Application boundaries

The core API retains 36 HTTP routes and their request parameters. Thirty-one route implementations are retained, including 28 database-backed readers plus liveness and metadata. Five private calculation rules/configuration routes return HTTP 501. Readers use actual stored data; a missing database or schema returns 503 rather than fabricated empty results. Carbon views read stored outputs without reconstructing private calculations. Private producer entry points cannot start.

The frontend retains the current institutional labels and presentation components. No Mapbox key or production data is included. Schema definitions and all 47 original Alembic revisions are retained unchanged.

## Local use

Use Python 3.12 and Node 22. Environment examples preserve the original setting names with placeholders only. Replace them with local values before using a new disposable database or stack.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r backend/requirements.txt
python3 -m uvicorn app.main:app --app-dir backend --host localhost --port 8000
```

The API serves `/health`, `/docs` and `/openapi.json` in development mode. In a second terminal, run `npm ci --ignore-scripts` and `npm run dev` in `frontend`. Its local proxy forwards `/api` to the backend. Database-backed views require a locally initialized database and locally supplied records; no mathematical fixture dataset is supplied.

Original Compose files and the security workflow remain available. Private workers and assessment commands are explicit unavailable boundaries. Do not use review placeholders as production settings.

## Review evidence

[Scope projection](docs/scope-projection.json) accounts for every original source path and identifies private boundaries. [Check results](docs/review-results.json) distinguish executed checks from unexecuted integration work. Check results list the validations run for this review. Full application integration testing requires a configured PostGIS database.

This sample does not grant an application-code license. Third-party terms remain applicable; see [notices](THIRD_PARTY_NOTICES.md). Publication follows client acceptance.
