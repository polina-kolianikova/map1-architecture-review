# Data model

The original PostgreSQL/PostGIS schema and all 47 Alembic revisions are retained under `backend/alembic`. This includes the real region, grid, source, observation, verification, audit, carbon-output, automation, cache, partition and index structures. Schema migrations are not replaced with an illustrative subset.

No production data or private coefficient configuration is included. Mathematical implementations are omitted separately from schema definitions. Migration graph checks do not replace actual application against a disposable PostGIS database.
