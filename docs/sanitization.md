# Sanitization boundary

Redactions cover proprietary calculation implementations, coefficient values, risk thresholds, private assessment fixtures, credentials, operational host/volume identities, original Git history and live server state. Ordinary source code, schema migrations, operational container structure and validation workflow are retained.

Numerical assessment code is replaced at named private boundaries. Five rules/configuration HTTP endpoints return 501. Stored-output readers do not load private coefficients or recompute omitted outputs. Missing database/schema errors are explicit; no fake empty response or model-result dataset is provided.

Original tests that contain private model inputs/outputs or depend on an omitted private assessment fixture are explicitly marked, rather than presented as passing model tests. Neutral HTTP, metadata, SQL projection and producer-boundary tests remain executable.

Environment values, operational hostname basenames and hardware-volume UUIDs are sanitized. No Mapbox key is supplied. No PDF or binary methodology document is included. The scanner covers the distributable source and a separate generated frontend artifact; dependency trees, local build/cache files and local verification artifacts are excluded from distribution.

Checks are bounded architecture checks, not a formal proof of absence or a claim that the entire original regression suite ran. Publication follows client acceptance.
