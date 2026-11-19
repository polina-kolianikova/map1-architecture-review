SET statement_timeout = 1800000;

WITH scope AS (
  SELECT id AS region_id, metadata
  FROM regions
  WHERE code = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
),
run AS (
  SELECT run.id, run.status, run.total_tiles, run.processed_tiles, run.inserted_cells, run.metadata
  FROM stage3_grid_generation_runs run, scope
  WHERE run.region_id = scope.region_id
    AND run.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
  ORDER BY id DESC
  LIMIT 1
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
component_metrics AS (
  SELECT
    count(*)::bigint AS component_fragments,
    count(*) FILTER (
      WHERE geom_projected IS NULL
         OR ST_IsEmpty(geom_projected)
         OR NOT ST_IsValid(geom_projected)
    )::bigint AS invalid_components,
    count(*) FILTER (
      WHERE COALESCE(NULLIF(component.metadata->>'connected_component_area_ha', '')::float, area_ha::float) < 1
    )::bigint AS tiny_components,
    COALESCE(sum(ST_Area(geom_projected)), 0)::float AS component_area_m2
  FROM stage3_region_mask_components component, scope
  WHERE component.region_id = scope.region_id
    AND component.mask_version = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
    AND component.projected_srid = 6933
),
cell_metrics AS (
  SELECT
    count(*)::bigint AS cell_count,
    count(*) FILTER (
      WHERE geom IS NULL
         OR ST_IsEmpty(geom)
         OR NOT ST_IsValid(geom)
    )::bigint AS invalid_cells,
    count(*) FILTER (WHERE GeometryType(geom) NOT IN ('POLYGON', 'MULTIPOLYGON'))::bigint AS nonpolygon_cells,
    count(*) FILTER (WHERE area_ha <= 0 OR area_ha > 1.0002)::bigint AS bad_area_cells,
    min(area_ha)::float AS min_area_ha,
    max(area_ha)::float AS max_area_ha,
    COALESCE(sum(area_ha), 0)::float AS stored_area_ha
  FROM grid_cells cell, scope
  WHERE cell.region_id = scope.region_id
    AND cell.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
),
sample_overlap AS (
  SELECT
    count(*) FILTER (WHERE overlap_m2 > 0.01)::bigint AS sampled_cells_with_exclusion_overlap,
    COALESCE(sum(overlap_m2), 0)::float AS sampled_exclusion_overlap_m2
  FROM (
    SELECT
      cell.id,
      COALESCE(sum(ST_Area(ST_Intersection(cell.geom, exclusion.geom_projected))), 0)::float AS overlap_m2
    FROM (
      SELECT cell.id, ST_Buffer(ST_Transform(cell.geom, 6933), -0.05) AS geom
      FROM grid_cells cell, scope
      WHERE cell.region_id = scope.region_id
        AND cell.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
      ORDER BY md5(id::text)
      LIMIT 2000
    ) cell
    LEFT JOIN stage3_source_mask_cache exclusion
      ON exclusion.source_region_code = 'TR-STAGE3-PUBLIC-FULL'
     AND exclusion.source_layer = 'osm_nonforest_exclusion'
     AND exclusion.projected_srid = 6933
     AND exclusion.geom_projected && cell.geom
     AND ST_Intersects(exclusion.geom_projected, cell.geom)
    WHERE cell.geom IS NOT NULL AND NOT ST_IsEmpty(cell.geom)
    GROUP BY cell.id
  ) hits
),
area_metrics AS (
  SELECT
    abs((SELECT component_area_m2 FROM component_metrics) / 10000.0 - (SELECT stored_area_ha FROM cell_metrics))::float AS stored_area_gap_ha
)
SELECT jsonb_pretty(
  jsonb_build_object(
    'region_code', 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10',
    'grid_version', 'stage3-public-ogm-osm-union-clean-v10',
    'run', (SELECT to_jsonb(run) FROM run),
    'tile_metrics', (SELECT to_jsonb(tile_metrics) FROM tile_metrics),
    'component_metrics', (SELECT to_jsonb(component_metrics) FROM component_metrics),
    'cell_metrics', (SELECT to_jsonb(cell_metrics) FROM cell_metrics),
    'sample_overlap', (SELECT to_jsonb(sample_overlap) FROM sample_overlap),
    'area_metrics', (SELECT to_jsonb(area_metrics) FROM area_metrics),
    'checks', jsonb_build_object(
      'ready', (
        (SELECT status = 'completed' AND total_tiles = processed_tiles FROM run)
        AND (SELECT tile_count = completed_tiles AND failed_tiles = 0 AND unfinished_tiles = 0 FROM tile_metrics)
        AND (SELECT inserted_cells = cell_count FROM run, cell_metrics)
        AND (SELECT invalid_components = 0 AND tiny_components = 0 AND component_fragments > 0 FROM component_metrics)
        AND (SELECT invalid_cells = 0 AND nonpolygon_cells = 0 AND bad_area_cells = 0 AND cell_count > 0 FROM cell_metrics)
        AND (SELECT sampled_cells_with_exclusion_overlap = 0 AND sampled_exclusion_overlap_m2 <= 1 FROM sample_overlap)
        AND (SELECT stored_area_gap_ha <= 5.0 FROM area_metrics)
      ),
      'run_completed', (SELECT status = 'completed' AND total_tiles = processed_tiles FROM run),
      'all_tiles_completed', (SELECT tile_count = completed_tiles AND failed_tiles = 0 AND unfinished_tiles = 0 FROM tile_metrics),
      'inserted_cells_match', (SELECT inserted_cells = cell_count FROM run, cell_metrics),
      'components_valid_and_above_1ha', (SELECT invalid_components = 0 AND tiny_components = 0 AND component_fragments > 0 FROM component_metrics),
      'cells_valid_polygonal_area_ok', (SELECT invalid_cells = 0 AND nonpolygon_cells = 0 AND bad_area_cells = 0 AND cell_count > 0 FROM cell_metrics),
      'sample_no_nonforest_overlap_after_5cm_interior_buffer', (SELECT sampled_cells_with_exclusion_overlap = 0 AND sampled_exclusion_overlap_m2 <= 1 FROM sample_overlap),
      'stored_area_gap_under_5ha', (SELECT stored_area_gap_ha <= 5.0 FROM area_metrics)
    )
  )
) AS report;
