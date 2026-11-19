SET statement_timeout = 1800000;

DROP TABLE IF EXISTS tmp_v9_expected_uuid;

CREATE TEMP TABLE tmp_v9_expected_uuid AS
WITH scope AS (
  SELECT id AS region_id
  FROM regions
  WHERE code = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
),
component_extent AS (
  SELECT
    floor((ST_XMin(ST_Extent(component.geom_projected)) - 100) / 10000)::int * 10000 AS min_x,
    floor((ST_YMin(ST_Extent(component.geom_projected)) - 100) / 10000)::int * 10000 AS min_y,
    ceil((ST_XMax(ST_Extent(component.geom_projected)) + 100) / 10000)::int * 10000 AS max_x,
    ceil((ST_YMax(ST_Extent(component.geom_projected)) + 100) / 10000)::int * 10000 AS max_y
  FROM stage3_region_mask_components component, scope
  WHERE component.region_id = scope.region_id
    AND component.mask_version = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
    AND component.projected_srid = 6933
),
tile_grid AS (
  SELECT x AS min_x, y AS min_y, x + 10000 AS max_x, y + 10000 AS max_y
  FROM component_extent,
       generate_series(min_x, max_x - 10000, 10000) AS x,
       generate_series(min_y, max_y - 10000, 10000) AS y
),
tiles AS (
  SELECT ST_MakeEnvelope(min_x, min_y, max_x, max_y, 6933) AS geom
  FROM tile_grid
),
tile_masks AS (
  SELECT
    row_number() OVER () AS tile_id,
    ST_UnaryUnion(ST_Collect(ST_Intersection(component.geom_projected, tile.geom))) AS geom
  FROM tiles tile
  JOIN scope ON true
  JOIN stage3_region_mask_components component
    ON component.region_id = scope.region_id
   AND component.mask_version = 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10'
   AND component.projected_srid = 6933
   AND component.geom_projected && tile.geom
   AND ST_Intersects(component.geom_projected, tile.geom)
  GROUP BY tile.geom
),
candidates AS (
  SELECT square.i, square.j, square.geom AS square_geom, mask.geom AS mask_geom
  FROM tile_masks mask,
       ST_SquareGrid(100, mask.geom) AS square
  WHERE square.geom && mask.geom
    AND ST_Intersects(square.geom, mask.geom)
),
clipped AS (
  SELECT
    i,
    j,
    ST_CollectionExtract(ST_MakeValid(ST_Intersection(square_geom, mask_geom)), 3) AS geom
  FROM candidates
)
SELECT DISTINCT
  (
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 1, 8) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 9, 4) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 13, 4) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 17, 4) || '-' ||
    substr(md5('TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10:stage3-public-ogm-osm-union-clean-v10:' || i::text || ':' || j::text), 21, 12)
  )::uuid AS uuid
FROM clipped
WHERE geom IS NOT NULL
  AND NOT ST_IsEmpty(geom)
  AND ST_Area(geom) > 0;

CREATE INDEX tmp_v9_expected_uuid_idx ON tmp_v9_expected_uuid(uuid);
ANALYZE tmp_v9_expected_uuid;

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
    (SELECT count(*) FROM tmp_v9_expected_uuid)::bigint AS expected_count,
    (SELECT count(*) FROM stored)::bigint AS stored_count,
    (SELECT count(*) FROM tmp_v9_expected_uuid expected LEFT JOIN stored USING (uuid) WHERE stored.uuid IS NULL)::bigint AS missing_stored_uuids,
    (SELECT count(*) FROM stored LEFT JOIN tmp_v9_expected_uuid expected USING (uuid) WHERE expected.uuid IS NULL)::bigint AS extra_stored_uuids
)
SELECT jsonb_pretty(
  jsonb_build_object(
    'region_code', 'TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10',
    'grid_version', 'stage3-public-ogm-osm-union-clean-v10',
    'run', (SELECT to_jsonb(run) FROM run),
    'uuid_metrics', (SELECT to_jsonb(uuid_metrics) FROM uuid_metrics),
    'checks', jsonb_build_object(
      'run_completed', (SELECT status = 'completed' AND total_tiles = processed_tiles FROM run),
      'uuid_sets_match', (SELECT expected_count = stored_count AND missing_stored_uuids = 0 AND extra_stored_uuids = 0 FROM uuid_metrics)
    )
  )
) AS report;
