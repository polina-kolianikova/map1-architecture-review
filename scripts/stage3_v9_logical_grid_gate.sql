SET statement_timeout = 1800000;

DROP TABLE IF EXISTS tmp_v9_expected_grid;

CREATE TEMP TABLE tmp_v9_expected_grid AS
WITH scope AS (
  SELECT id AS region_id
  FROM regions
  WHERE code = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
),
tiles AS (
  SELECT id, ST_MakeEnvelope(min_x, min_y, max_x, max_y, 6933) AS geom
  FROM stage3_grid_generation_tiles
  WHERE run_id = (
    SELECT run.id
    FROM stage3_grid_generation_runs run, scope
    WHERE run.region_id = scope.region_id
      AND run.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
    ORDER BY run.id DESC
    LIMIT 1
  )
    AND status = 'completed'
),
tile_masks AS (
  SELECT
    tile.id AS tile_id,
    ST_UnaryUnion(ST_Collect(ST_Intersection(component.geom_projected, tile.geom))) AS geom
  FROM tiles tile
  JOIN scope ON true
  JOIN stage3_region_mask_components component
    ON component.region_id = scope.region_id
   AND component.mask_version = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
   AND component.projected_srid = 6933
   AND component.geom_projected && tile.geom
   AND ST_Intersects(component.geom_projected, tile.geom)
  GROUP BY tile.id
),
candidates AS (
  SELECT
    mask.tile_id,
    square.i,
    square.j,
    square.geom AS square_geom,
    mask.geom AS mask_geom
  FROM tile_masks mask,
       ST_SquareGrid(100, mask.geom) AS square
  WHERE square.geom && mask.geom
    AND ST_Intersects(square.geom, mask.geom)
),
clipped AS (
  SELECT
    tile_id,
    i,
    j,
    ST_CollectionExtract(ST_MakeValid(ST_Intersection(square_geom, mask_geom)), 3) AS geom
  FROM candidates
)
SELECT DISTINCT ON (i, j)
  (
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 1, 8) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 9, 4) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 13, 4) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 17, 4) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 21, 12)
  )::uuid AS uuid,
  tile_id,
  i,
  j,
  geom
FROM clipped
WHERE geom IS NOT NULL
  AND NOT ST_IsEmpty(geom)
  AND ST_Area(geom) > 0
ORDER BY i, j, tile_id;

CREATE INDEX tmp_v9_expected_grid_uuid_idx ON tmp_v9_expected_grid(uuid);
CREATE INDEX tmp_v9_expected_grid_tile_idx ON tmp_v9_expected_grid(tile_id);
ANALYZE tmp_v9_expected_grid;

WITH scope AS (
  SELECT id AS region_id
  FROM regions
  WHERE code = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
),
stored AS (
  SELECT cell.uuid
  FROM grid_cells cell, scope
  WHERE cell.region_id = scope.region_id
    AND cell.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
),
run AS (
  SELECT status, total_tiles, processed_tiles, inserted_cells, id
  FROM stage3_grid_generation_runs run, scope
  WHERE run.region_id = scope.region_id
    AND run.grid_version = 'stage3-public-ogm-osm-union-clean-v10'
  ORDER BY id DESC
  LIMIT 1
),
uuid_metrics AS (
  SELECT
    (SELECT count(*) FROM tmp_v9_expected_grid)::bigint AS expected_count,
    (SELECT count(*) FROM stored)::bigint AS stored_count,
    (SELECT count(*) FROM tmp_v9_expected_grid expected LEFT JOIN stored USING (uuid) WHERE stored.uuid IS NULL)::bigint AS missing_stored_uuids,
    (SELECT count(*) FROM stored LEFT JOIN tmp_v9_expected_grid expected USING (uuid) WHERE expected.uuid IS NULL)::bigint AS extra_stored_uuids
),
tile_diff AS (
  WITH tiles AS (
    SELECT id, ST_MakeEnvelope(min_x, min_y, max_x, max_y, 6933) AS geom
    FROM stage3_grid_generation_tiles
    WHERE run_id = (SELECT id FROM run)
      AND status = 'completed'
  ),
  masks AS (
    SELECT
      tile.id AS tile_id,
      ST_UnaryUnion(ST_Collect(ST_Intersection(component.geom_projected, tile.geom))) AS geom
    FROM tiles tile
    JOIN scope ON true
    JOIN stage3_region_mask_components component
      ON component.region_id = scope.region_id
     AND component.mask_version = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
     AND component.projected_srid = 6933
     AND component.geom_projected && tile.geom
     AND ST_Intersects(component.geom_projected, tile.geom)
    GROUP BY tile.id
  ),
  cells AS (
    SELECT tile_id, ST_UnaryUnion(ST_Collect(geom)) AS geom
    FROM tmp_v9_expected_grid
    GROUP BY tile_id
  )
  SELECT
    COALESCE(sum(ST_Area(ST_Difference(mask.geom, cell.geom))), 0)::float AS exact_mask_missing_m2,
    COALESCE(sum(ST_Area(ST_Difference(cell.geom, mask.geom))), 0)::float AS exact_cells_outside_m2,
    count(*) FILTER (
      WHERE ST_Area(ST_Difference(mask.geom, cell.geom)) > 0.001
         OR ST_Area(ST_Difference(cell.geom, mask.geom)) > 0.001
    )::bigint AS bad_tiles
  FROM masks mask
  LEFT JOIN cells cell USING (tile_id)
)
SELECT jsonb_pretty(
  jsonb_build_object(
    'region_code', 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10',
    'grid_version', 'stage3-public-ogm-osm-union-clean-v10',
    'run', (SELECT to_jsonb(run) FROM run),
    'uuid_metrics', (SELECT to_jsonb(uuid_metrics) FROM uuid_metrics),
    'tile_diff', (SELECT to_jsonb(tile_diff) FROM tile_diff),
    'checks', jsonb_build_object(
      'run_completed', (SELECT status = 'completed' AND total_tiles = processed_tiles FROM run),
      'uuid_sets_match', (SELECT expected_count = stored_count AND missing_stored_uuids = 0 AND extra_stored_uuids = 0 FROM uuid_metrics),
      'exact_projected_coverage', (SELECT exact_mask_missing_m2 <= 1 AND exact_cells_outside_m2 <= 1 AND bad_tiles = 0 FROM tile_diff)
    )
  )
) AS report;
