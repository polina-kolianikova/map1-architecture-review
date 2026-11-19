SET statement_timeout = 1800000;

WITH scope AS (
  SELECT id AS region_id
  FROM regions
  WHERE code = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
),
run AS (
  SELECT id, status, total_tiles, processed_tiles, inserted_cells
  FROM stage3_grid_generation_runs run, scope
  WHERE run.region_id = scope.region_id
    AND run.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
  ORDER BY id DESC
  LIMIT 1
),
component_metrics AS (
  SELECT
    count(*)::bigint AS component_fragments,
    COALESCE(sum(ST_Area(geom_projected)), 0)::float AS component_area_m2,
    count(*) FILTER (
      WHERE geom_projected IS NULL
         OR ST_IsEmpty(geom_projected)
         OR NOT ST_IsValid(geom_projected)
    )::bigint AS invalid_components
  FROM stage3_region_mask_components component, scope
  WHERE component.region_id = scope.region_id
    AND component.mask_version = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
    AND component.projected_srid = 6933
),
cell_metrics AS (
  SELECT
    count(*)::bigint AS cell_count,
    COALESCE(sum(area_ha), 0)::float AS stored_area_ha,
    count(*) FILTER (
      WHERE geom IS NULL
         OR ST_IsEmpty(geom)
         OR NOT ST_IsValid(geom)
    )::bigint AS invalid_cells,
    min(area_ha)::float AS min_area_ha,
    max(area_ha)::float AS max_area_ha
  FROM grid_cells cell, scope
  WHERE cell.region_id = scope.region_id
    AND cell.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
),
tile_metrics AS (
  SELECT
    count(tile.id)::bigint AS tile_count,
    count(tile.id) FILTER (WHERE tile.status = 'completed')::bigint AS completed_tiles,
    count(tile.id) FILTER (WHERE tile.status = 'failed')::bigint AS failed_tiles,
    count(tile.id) FILTER (WHERE tile.status IN ('pending', 'running'))::bigint AS unfinished_tiles
  FROM run
  LEFT JOIN stage3_grid_generation_tiles tile ON tile.run_id = run.id
),
area_metrics AS (
  SELECT
    abs((SELECT component_area_m2 FROM component_metrics) / 10000.0 - (SELECT stored_area_ha FROM cell_metrics))::float AS area_gap_ha
)
SELECT jsonb_pretty(
  jsonb_build_object(
    'region_code', 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10',
    'grid_version', 'stage3-public-ogm-osm-union-clean-v10',
    'run', (SELECT to_jsonb(run) FROM run),
    'component_metrics', (SELECT to_jsonb(component_metrics) FROM component_metrics),
    'cell_metrics', (SELECT to_jsonb(cell_metrics) FROM cell_metrics),
    'tile_metrics', (SELECT to_jsonb(tile_metrics) FROM tile_metrics),
    'area_metrics', (SELECT to_jsonb(area_metrics) FROM area_metrics),
    'checks', jsonb_build_object(
      'run_completed', (SELECT status = 'completed' AND total_tiles = processed_tiles FROM run),
      'all_tiles_completed', (SELECT tile_count = completed_tiles AND failed_tiles = 0 AND unfinished_tiles = 0 FROM tile_metrics),
      'inserted_cells_match', (SELECT inserted_cells = cell_count FROM run, cell_metrics),
      'cells_valid', (SELECT invalid_cells = 0 FROM cell_metrics),
      'components_valid', (SELECT invalid_components = 0 FROM component_metrics),
      'area_gap_under_0_001ha', (SELECT area_gap_ha <= 0.001 FROM area_metrics),
      'cell_area_cap_ok', (SELECT max_area_ha <= 1.0002 FROM cell_metrics)
    )
  )
) AS report;
