# Source structure

The original `backend`, `frontend`, `infra`, `docs`, `scripts` and `.github/workflows` directory and file structure is retained. The per-file mapping is in `scope-projection.json`.

Ordinary implementation files are retained unchanged where possible. Secret host/account/volume settings use explicit placeholders. Dedicated private assessment modules and producers retain named unavailable boundaries. Private numerical test fixtures and dependent expectations are omitted explicitly, with neutral contract tests provided separately. Public spatial, storage, source-adapter, audit and queue structure is retained.

The original Alembic chain remains in `backend/alembic/versions`. The top-level `migrations` directory is only a navigation reference and does not replace that chain.
