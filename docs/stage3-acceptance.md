# Stage 3 V10 acceptance

This document supersedes the old V7/V8/V9 public-mask notes.

Do not run the full Turkey V10 generation until the user explicitly approves
scaling. The commands are locked in code and require:

```bash
--ack-full-turkey-user-approved
```

## Client formula

The only accepted public Stage 3 V10 formula is:

```text
ogm_esatis_bolme ∪ osm_forest_mask − osm_nonforest_exclusion
```

Then:

1. drop final connected components `< 1 ha`;
2. generate the 100 m grid only inside that final mask;
3. run proof gates;
4. run browser/visual QA screenshots.

This is public-data-only evidence. Official closed Layer A/cadastre remains a
future dependency.

## Canonical identifiers

Pilot:

```text
region_code  = TR-STAGE3-CLIENT-CLEAN-PILOT-DEVREKANI
grid_version = stage3-client-clean-pilot-devrekani-union-v5
```

Full Turkey, after approval only:

```text
region_code  = TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10
grid_version = stage3-public-ogm-osm-union-clean-v10
```

Source scope:

```text
source_region_code = TR-STAGE3-PUBLIC-FULL
```

## Pilot evidence

The current pilot evidence is stored in:

```text
test-results/stage3-client-clean-pilot-qa/
```

Required pilot artifacts:

- `formula_equivalence.json`
- `forest_clip.json`
- `grid_mask_proof.json`
- `grid_spatial_integrity.json`
- `stage3-public-v10-*.png`

The pilot is ready for scaling only when all proof reports are green and the
screenshots have been reviewed visually.

## Full Turkey run order

Run these only after explicit user approval.

If generating the runbook with `run_stage3_contract_plan`, pass
`--ack-full-turkey-user-approved` only after that approval so the emitted mask
and grid commands carry the same guard flag.

Prepare/refresh the source cache:

```bash
python -m app.commands.prepare_stage3_source_mask_cache \
  --source-region-code TR-STAGE3-PUBLIC-FULL \
  --source-layers ogm_esatis_bolme,osm_forest_mask,osm_nonforest_exclusion \
  --projected-srid 6933 \
  --replace \
  --analyze \
  --ack-full-turkey-user-approved
```

Prepare the final clean forest mask from the exact client formula:

```bash
python -m app.commands.run_stage3_prepare_region_fragments \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --region-name "Stage 3 V10 public OGM plus OSM forest clean full" \
  --source-region-code TR-STAGE3-PUBLIC-FULL \
  --clip-region-code TR-STAGE3-PUBLIC-FULL \
  --source-layers ogm_esatis_bolme,osm_forest_mask \
  --difference-mask-source-layers osm_nonforest_exclusion \
  --projected-srid 6933 \
  --tile-size-m 10000 \
  --min-area-ha 1 \
  --max-area-ha 30000000 \
  --min-mask-component-area-ha 1.0 \
  --snap-grid-m 0 \
  --edge-tolerance-m 0.02 \
  --subdivide-max-vertices 256 \
  --ack-full-turkey-user-approved
```

Generate the grid:

```bash
python -m app.commands.run_stage3_grid_generation_batched \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --grid-size-m 100 \
  --projected-srid 6933 \
  --tile-size-m 10000 \
  --max-failed-tiles 0 \
  --use-region-mask-components \
  --min-mask-component-area-ha 1.0 \
  --ack-full-turkey-user-approved
```

Worker resumes must use the same command plus:

```bash
--resume
```

## Required full proof gates

```bash
python -m app.commands.check_stage3_forest_clip \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --output /app/artifacts/stage3-v10-forest-clip.json

python scripts/stage3_v9_fragment_equivalence_gate.py \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --source-region-code TR-STAGE3-PUBLIC-FULL \
  --clip-region-code TR-STAGE3-PUBLIC-FULL \
  --mask-version TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --source-layers ogm_esatis_bolme,osm_forest_mask \
  --exclusion-source-layers osm_nonforest_exclusion \
  --projected-srid 6933 \
  --tile-size-m 10000 \
  --snap-grid-m 0 \
  --subdivide-max-vertices 256 \
  --min-component-area-ha 1.0 \
  --max-symdiff-area-m2 1.0 \
  --max-exclusion-overlap-m2 1.0 \
  --max-outside-region-m2 1.0 \
  --output /app/artifacts/stage3-v10-fragment-formula-equivalence.json

python -m app.commands.check_stage3_grid_mask_proof \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --mask-version TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --projected-srid 6933 \
  --min-component-area-ha 1.0 \
  --output /app/artifacts/stage3-v10-grid-mask-proof.json

python -m app.commands.check_stage3_grid_spatial_integrity \
  --region-code TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --grid-version stage3-public-ogm-osm-union-clean-v10 \
  --mask-version TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10 \
  --projected-srid 6933 \
  --output /app/artifacts/stage3-v10-grid-spatial-integrity.json
```

The full run is client-ready only after:

- full mask exists;
- full grid exists;
- all full gates are green;
- frontend/backend are deployed with V10 explicit scope;
- browser QA passes on live;
- whole-map and regional screenshots are reviewed visually.
