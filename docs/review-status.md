# Architecture review scope

All original source directories and file paths in the reviewed project are retained. Ordinary source implementations are included. Private calculation producers/functions, private configuration values and mathematical fixtures have explicit replacement boundaries. No source file is omitted merely because its review was deferred.

The original 47-revision migration chain is unchanged. Original infrastructure and CI structure are retained with environment and host/volume settings sanitized. The API retains ordinary stored-data readers; only five private rules/configuration endpoints return 501. Private processing cannot be started from its protected entry points.

See `scope-projection.json` for the per-file mapping and `review-results.json` for executed checks. Full original CI execution and live PostGIS migration/data qualification are outside the executed architecture checks and are not claimed.

Publication follows client acceptance.
