from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.api.stage1 import stage1_params
from app.db.session import SessionLocal
from app.services.stage3_partitioned_reads import (
    V10_GRID_VERSION,
    V10_REGION_CODE,
    sentinel_observations_table_sql,
    verification_results_table_sql,
)

GRID_CELLS_TABLES = ("grid_cells", "grid_cells_partitioned")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Stage 3 read performance baseline")
    parser.add_argument("--region-code", default="")
    parser.add_argument("--grid-version", default="")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-avg-ms", type=float, default=0)
    parser.add_argument("--max-p95-ms", type=float, default=0)
    parser.add_argument("--trend-months", type=int, default=12)
    parser.add_argument("--projected-srid", type=int, default=32636)
    parser.add_argument("--tile-z", type=int, default=12)
    parser.add_argument("--tile-x", type=int)
    parser.add_argument("--tile-y", type=int)
    parser.add_argument("--output", default="")
    parser.add_argument("--grid-cells-table", choices=GRID_CELLS_TABLES, default="grid_cells")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--explain-analyze", action="store_true")
    parser.add_argument("--analyze-tables", action="store_true")
    parser.add_argument("--fail-on-seq-scan", action="store_true")
    parser.add_argument(
        "--allow-staged-evidence",
        action="store_true",
        help="Skip Layer B/C and carbon probes when the selected rollout has not loaded those evidence tables yet.",
    )
    args = parser.parse_args()
    if args.iterations <= 0:
        raise ValueError("iterations must be greater than zero")
    if args.limit <= 0:
        raise ValueError("limit must be greater than zero")
    if args.grid_cells_table == "grid_cells_partitioned" and (
        args.region_code != V10_REGION_CODE or args.grid_version != V10_GRID_VERSION
    ):
        parser.error(
            "grid_cells_partitioned performance probes are restricted to the exact V10 public scope"
        )

    probes = [
        QueryProbe("asset_register_page", _asset_register_sql, {"limit": args.limit}),
        QueryProbe("asset_register_count", _asset_count_sql, {}),
        QueryProbe("latest_ndvi_history", _latest_ndvi_history_sql, {"limit": args.limit, "trend_months": args.trend_months}),
        QueryProbe(
            "layer_a_intersections",
            _layer_a_intersection_sql,
            {"limit": args.limit, "projected_srid": args.projected_srid},
        ),
        QueryProbe(
            "raw_grid_tile_window",
            _raw_grid_tile_window_sql,
            {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y},
        ),
        QueryProbe(
            "layer_b_tile_window",
            _layer_b_tile_window_sql,
            {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y},
        ),
        QueryProbe(
            "layer_c_tile_window",
            _layer_c_tile_window_sql,
            {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y},
        ),
        QueryProbe(
            "raw_grid_mvt_tile",
            _raw_grid_mvt_sql,
            {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y},
        ),
        QueryProbe(
            "layer_b_mvt_tile",
            _layer_b_mvt_sql,
            {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y},
        ),
        QueryProbe(
            "layer_c_mvt_tile",
            _layer_c_mvt_sql,
            {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y},
        ),
        QueryProbe("carbon_asset_page", _carbon_asset_page_sql, {"limit": args.limit}),
        QueryProbe("carbon_audit_tail", _carbon_audit_tail_sql, {"limit": args.limit}),
    ]

    with SessionLocal() as db:
        params = stage1_params(region_code=args.region_code or None, grid_version=args.grid_version or None, db=db)
        tile = _selected_tile(db, params, args)
    args.tile_z = tile["z"]
    args.tile_x = tile["x"]
    args.tile_y = tile["y"]
    for probe in probes:
        if "z" in probe.params:
            probe.params.update({"z": args.tile_z, "x": args.tile_x, "y": args.tile_y})
    report: dict[str, Any] = {
        "stage": "stage3",
        "scope": {
            "region_code": params["region_code"],
            "grid_version": params["grid_version"],
            "grid_cells_table": args.grid_cells_table,
            "tile": tile,
        },
        "iterations": args.iterations,
        "limit": args.limit,
        "trend_months": args.trend_months,
        "max_avg_ms": args.max_avg_ms or None,
        "max_p95_ms": args.max_p95_ms or None,
        "probes": [],
    }
    failed = []
    with SessionLocal() as db:
        if args.analyze_tables:
            _analyze_tables(db, grid_table=args.grid_cells_table)
        report["table_counts"] = _table_counts(db, params, grid_table=args.grid_cells_table)
        staged_skips = _staged_probe_skips(args, report["table_counts"])
        for probe in probes:
            if probe.name in staged_skips:
                report["probes"].append(
                    {
                        "name": probe.name,
                        "skipped": True,
                        "reason": staged_skips[probe.name],
                        "result_count": 0,
                    }
                )
                continue
            probe_params = {**params, **probe.params}
            samples = [
                _time_query(db, probe.sql(args.grid_cells_table), probe_params)
                for _ in range(args.iterations)
            ]
            timings = [sample.elapsed_ms for sample in samples]
            result_count = max(sample.result_count for sample in samples)
            entry = {
                "name": probe.name,
                "min_ms": round(min(timings), 3),
                "p50_ms": round(_percentile(timings, 0.50), 3),
                "p95_ms": round(_percentile(timings, 0.95), 3),
                "max_ms": round(max(timings), 3),
                "avg_ms": round(sum(timings) / len(timings), 3),
                "result_count": result_count,
            }
            if args.explain or args.explain_analyze:
                entry["plan"] = _explain(
                    db,
                    probe.sql(args.grid_cells_table),
                    probe_params,
                    analyze=args.explain_analyze,
                )
                if args.fail_on_seq_scan and _has_seq_scan(entry["plan"]):
                    failed.append(f"{probe.name}:seq_scan")
            report["probes"].append(entry)
            if args.max_avg_ms and entry["avg_ms"] > args.max_avg_ms:
                failed.append(f"{probe.name}:avg_ms")
            if args.max_p95_ms and entry["p95_ms"] > args.max_p95_ms:
                failed.append(f"{probe.name}:p95_ms")
            if result_count <= 0:
                failed.append(f"{probe.name}:empty_result")
    report["status"] = "failed" if failed else "ok"
    report["failed_probes"] = failed
    payload = json.dumps(report, indent=2)
    if args.output:
        write_report(Path(args.output), payload)
    print(payload)
    if failed:
        raise SystemExit(f"Stage 3 performance baseline exceeded max avg ms: {', '.join(failed)}")


def _staged_probe_skips(args, table_counts: dict[str, int]) -> dict[str, str]:
    if not args.allow_staged_evidence:
        return {}
    skips = {}
    if table_counts.get("ndvi_observations", 0) <= 0:
        reason = "NDVI evidence is staged for this rollout"
        skips["latest_ndvi_history"] = reason
        skips["layer_b_tile_window"] = reason
        skips["layer_b_mvt_tile"] = reason
    if table_counts.get("verification_results", 0) <= 0:
        reason = "verification evidence is staged for this rollout"
        skips["layer_c_tile_window"] = reason
        skips["layer_c_mvt_tile"] = reason
    if table_counts.get("carbon_assessments", 0) <= 0:
        skips["carbon_asset_page"] = "carbon evidence is staged for this rollout"
    if table_counts.get("carbon_audit_events", 0) <= 0:
        skips["carbon_audit_tail"] = "carbon audit evidence is staged for this rollout"
    return skips


def write_report(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")


def _selected_tile(db, params: dict[str, Any], args) -> dict[str, int]:
    if (args.tile_x is None) != (args.tile_y is None):
        raise ValueError("--tile-x and --tile-y must be supplied together")
    if args.tile_x is not None and args.tile_y is not None:
        return {"z": args.tile_z, "x": args.tile_x, "y": args.tile_y, "source": "args"}
    sentinel_table = sentinel_observations_table_sql()
    verification_table = verification_results_table_sql()
    evidence_row = db.execute(
        text(
            f"""
            SELECT
                ST_X(ST_PointOnSurface(cell.geom)) AS lon,
                ST_Y(ST_PointOnSurface(cell.geom)) AS lat
            FROM {getattr(args, "grid_cells_table", "grid_cells")} cell
            JOIN regions region ON region.id = cell.region_id
            JOIN {sentinel_table} observation
              ON observation.grid_cell_id = cell.id
             AND observation.region_id = cell.region_id
             AND observation.is_current
             AND observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
             AND observation.region_code = :region_code
             AND observation.grid_version = :grid_version
            JOIN {verification_table} result
              ON result.grid_cell_id = cell.id
             AND result.region_id = cell.region_id
             AND result.is_current
             AND result.metadata->>'pipeline' = 'stage1_verification'
             AND result.region_code = :region_code
             AND result.grid_version = :grid_version
            WHERE region.code = :region_code
              AND cell.grid_version = :grid_version
            ORDER BY cell.id
            LIMIT 1
            """
        ),
        params,
    ).mappings().first()
    if evidence_row is not None:
        x, y = _lonlat_to_tile(float(evidence_row["lon"]), float(evidence_row["lat"]), args.tile_z)
        return {"z": args.tile_z, "x": x, "y": y, "source": "current_evidence_cell"}
    row = db.execute(
        text(
            """
            SELECT
                ST_X(ST_PointOnSurface(geom)) AS lon,
                ST_Y(ST_PointOnSurface(geom)) AS lat
            FROM regions
            WHERE code = :region_code
            """
        ),
        params,
    ).mappings().first()
    if row is None:
        raise SystemExit(
            f"region not found: {params['region_code']}. Run run_stage3_prepare_region first."
        )
    x, y = _lonlat_to_tile(float(row["lon"]), float(row["lat"]), args.tile_z)
    return {"z": args.tile_z, "x": x, "y": y, "source": "region_centroid"}


def _lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


class QueryProbe:
    def __init__(self, name: str, sql: Callable[[str], str], params: dict[str, Any]) -> None:
        self.name = name
        self.sql = sql
        self.params = params


class QuerySample:
    def __init__(self, elapsed_ms: float, result_count: int) -> None:
        self.elapsed_ms = elapsed_ms
        self.result_count = result_count


def _time_query(db, sql: str, params: dict[str, Any]) -> QuerySample:
    started = time.perf_counter()
    rows = list(db.execute(text(sql), params).mappings())
    elapsed_ms = (time.perf_counter() - started) * 1000
    return QuerySample(elapsed_ms, _result_count(rows))


def _result_count(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    if len(rows) == 1:
        values = list(rows[0].values())
        if len(values) == 1 and isinstance(values[0], (int, float)):
            return int(values[0] or 0)
    return len(rows)


def _explain(db, sql: str, params: dict[str, Any], analyze: bool = False) -> list[str]:
    mode = "ANALYZE, BUFFERS, FORMAT TEXT" if analyze else "FORMAT TEXT"
    rows = db.execute(text(f"EXPLAIN ({mode}) {sql}"), params).all()
    return [row[0] for row in rows]


def _analyze_tables(db, grid_table: str = "grid_cells") -> None:
    sentinel_table = sentinel_observations_table_sql()
    verification_table = verification_results_table_sql()
    for table_name in (
        "regions",
        grid_table,
        "ogm_parcels",
        sentinel_table,
        verification_table,
        "carbon_assessments",
    ):
        db.execute(text(f"ANALYZE {table_name}"))


def _table_counts(db, params: dict[str, Any], grid_table: str = "grid_cells") -> dict[str, int]:
    sentinel_table = sentinel_observations_table_sql()
    verification_table = verification_results_table_sql()
    return dict(
        db.execute(
            text(
                f"""
                SELECT
                    (
                        SELECT count(*)
                        FROM {grid_table} cell
                        JOIN regions region ON region.id = cell.region_id
                        WHERE region.code = :region_code
                          AND cell.grid_version = :grid_version
                    ) AS grid_cells,
                    (
                        SELECT count(*)
	                    FROM ogm_parcels parcel
	                    WHERE parcel.is_current
	                      AND parcel.attributes->>'region_code' = :layer_a_region_code
	                      AND parcel.source_layer = ANY(:layer_a_source_layers)
                    ) AS layer_a_features,
                    (
                        SELECT count(*)
                        FROM {sentinel_table} observation
                        WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                          AND observation.is_current
                          AND observation.ndvi IS NOT NULL
                          AND COALESCE(observation.metadata->>'status', 'ok') = 'ok'
                          AND observation.metadata->>'statistics_source' = 'sentinel_hub_statistical_api'
                          AND observation.region_id = (
                              SELECT id
                              FROM regions
                              WHERE code = :region_code
                          )
                          AND observation.region_code = :region_code
                          AND observation.grid_version = :grid_version
                    ) AS ndvi_observations
                    ,
                    (
                        SELECT count(*)
                        FROM {verification_table} result
                        WHERE result.is_current
                          AND result.metadata->>'pipeline' = 'stage1_verification'
                          AND result.region_id = (
                              SELECT id
                              FROM regions
                              WHERE code = :region_code
                          )
                          AND result.region_code = :region_code
                          AND result.grid_version = :grid_version
                    ) AS verification_results
                    ,
                    (
                        SELECT count(*)
                        FROM carbon_assessments assessment
                        WHERE assessment.is_current
                          AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
                          AND EXISTS (
                              SELECT 1
                              FROM {grid_table} cell
                              JOIN regions region ON region.id = cell.region_id
                              WHERE cell.id = assessment.grid_cell_id
                                AND region.code = :region_code
                                AND cell.grid_version = :grid_version
                          )
                    ) AS carbon_assessments
                    ,
                    (
                        SELECT count(*)
                        FROM carbon_assessment_events event
                        WHERE event.metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
                          AND EXISTS (
                              SELECT 1
                              FROM {grid_table} cell
                              JOIN regions region ON region.id = cell.region_id
                              WHERE cell.id = event.grid_cell_id
                                AND region.code = :region_code
                                AND cell.grid_version = :grid_version
                          )
                    ) AS carbon_audit_events
                """
            ),
            params,
        ).mappings().one()
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _has_seq_scan(plan: list[str]) -> bool:
    return any("Seq Scan" in row for row in plan)


def _asset_register_sql(grid_table: str = "grid_cells") -> str:
    verification_table = verification_results_table_sql()
    return f"""
        WITH region_scope AS (
            SELECT id
            FROM regions
            WHERE code = :region_code
        ),
        scoped_cells AS MATERIALIZED (
            SELECT
                cell.id,
                cell.uuid,
                cell.region_id
            FROM {grid_table} cell
            WHERE cell.region_id = (SELECT id FROM region_scope)
              AND cell.grid_version = :grid_version
            ORDER BY cell.id
            LIMIT :limit
        ),
        latest_result AS (
            SELECT DISTINCT ON (result.grid_cell_id)
                result.grid_cell_id,
                result.anomaly_type,
                result.score,
                result.created_at
            FROM {verification_table} result
            JOIN scoped_cells cell ON cell.id = result.grid_cell_id
            WHERE result.is_current
              AND result.metadata->>'pipeline' = 'stage1_verification'
              AND result.region_code = :region_code
              AND result.grid_version = :grid_version
            ORDER BY result.grid_cell_id, result.created_at DESC, result.id DESC
        )
        SELECT
            cell.uuid::text AS cell_uuid,
            COALESCE(latest_result.anomaly_type, 'unverified') AS status,
            latest_result.score
        FROM scoped_cells cell
        LEFT JOIN latest_result ON latest_result.grid_cell_id = cell.id
        ORDER BY cell.id
    """


def _asset_count_sql(grid_table: str = "grid_cells") -> str:
    return f"""
        SELECT COALESCE(
            (
                SELECT run.inserted_cells
                FROM stage3_grid_generation_runs run
                WHERE run.region_id = (
                      SELECT id
                      FROM regions
                      WHERE code = :region_code
                  )
                  AND run.grid_version = :grid_version
                  AND run.status = 'completed'
                  AND run.inserted_cells IS NOT NULL
                ORDER BY run.finished_at DESC NULLS LAST, run.id DESC
                LIMIT 1
            ),
            (
                SELECT count(*)
                FROM {grid_table} cell
                WHERE cell.region_id = (
                      SELECT id
                      FROM regions
                      WHERE code = :region_code
                  )
                  AND cell.grid_version = :grid_version
            )
        ) AS total
    """


def _latest_ndvi_history_sql(grid_table: str = "grid_cells") -> str:
    sentinel_table = sentinel_observations_table_sql()
    return f"""
        SELECT
            observation.grid_cell_id,
            observation.acquisition_date,
            observation.ndvi
        FROM {sentinel_table} observation
        JOIN {grid_table} cell ON cell.id = observation.grid_cell_id
        JOIN regions region ON region.id = cell.region_id
        WHERE observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
          AND observation.ndvi IS NOT NULL
          AND COALESCE(observation.metadata->>'status', CASE WHEN observation.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
          AND observation.metadata->>'statistics_source' = 'sentinel_hub_statistical_api'
          AND observation.region_code = :region_code
          AND observation.grid_version = :grid_version
          AND region.code = :region_code
          AND cell.grid_version = :grid_version
          AND observation.acquisition_date >= (
              SELECT COALESCE(max(anchor.acquisition_date), CURRENT_DATE)
              FROM {sentinel_table} anchor
              JOIN {grid_table} anchor_cell ON anchor_cell.id = anchor.grid_cell_id
              JOIN regions anchor_region ON anchor_region.id = anchor_cell.region_id
              WHERE anchor.metadata->>'pipeline' = 'stage1_cdse_ndvi'
                AND anchor.region_code = :region_code
                AND anchor.grid_version = :grid_version
                AND anchor.ndvi IS NOT NULL
                AND COALESCE(anchor.metadata->>'status', CASE WHEN anchor.ndvi IS NOT NULL THEN 'ok' END) = 'ok'
                AND anchor.metadata->>'statistics_source' = 'sentinel_hub_statistical_api'
                AND anchor_region.code = :region_code
                AND anchor_cell.grid_version = :grid_version
          ) - (:trend_months * INTERVAL '1 month')
        ORDER BY observation.acquisition_date DESC, observation.id DESC
        LIMIT :limit
    """


def _layer_a_intersection_sql(grid_table: str = "grid_cells") -> str:
    return f"""
        SELECT
            cell.uuid::text AS cell_uuid,
            parcel.source_layer,
            ST_Area(ST_Transform(ST_Intersection(cell.geom, parcel.geom), :projected_srid)) / 10000.0 AS area_ha
        FROM {grid_table} cell
        JOIN regions region ON region.id = cell.region_id
        JOIN ogm_parcels parcel
          ON parcel.is_current
	         AND parcel.source_layer = ANY(:layer_a_source_layers)
	         AND parcel.attributes->>'region_code' = :layer_a_region_code
	         AND parcel.geom && cell.geom
         AND ST_Intersects(parcel.geom, cell.geom)
        WHERE region.code = :region_code
          AND cell.grid_version = :grid_version
        LIMIT :limit
    """


def _raw_grid_tile_window_sql(grid_table: str = "grid_cells") -> str:
    return f"""
        WITH bounds AS (
            SELECT ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        )
        SELECT count(*) AS cell_count
        FROM {grid_table} cell
        JOIN regions region ON region.id = cell.region_id
        CROSS JOIN bounds
        WHERE region.code = :region_code
          AND cell.grid_version = :grid_version
          AND cell.geom && bounds.geom_4326
          AND ST_Intersects(cell.geom, bounds.geom_4326)
    """


def _layer_b_tile_window_sql(grid_table: str = "grid_cells") -> str:
    sentinel_table = sentinel_observations_table_sql()
    return f"""
        WITH bounds AS (
            SELECT ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        ),
        tile_cells AS MATERIALIZED (
            SELECT cell.id, cell.region_id
            FROM {grid_table} cell
            JOIN regions region ON region.id = cell.region_id
            CROSS JOIN bounds
            WHERE region.code = :region_code
              AND cell.grid_version = :grid_version
              AND cell.geom && bounds.geom_4326
              AND ST_Intersects(cell.geom, bounds.geom_4326)
        )
        SELECT count(*) AS current_signal_cells
        FROM tile_cells cell
        JOIN {sentinel_table} observation
          ON observation.grid_cell_id = cell.id
         AND observation.region_id = cell.region_id
         AND observation.is_current
         AND observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
         AND observation.region_code = :region_code
         AND observation.grid_version = :grid_version
    """


def _layer_c_tile_window_sql(grid_table: str = "grid_cells") -> str:
    verification_table = verification_results_table_sql()
    return f"""
        WITH bounds AS (
            SELECT ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        ),
        tile_cells AS MATERIALIZED (
            SELECT cell.id, cell.region_id
            FROM {grid_table} cell
            JOIN regions region ON region.id = cell.region_id
            CROSS JOIN bounds
            WHERE region.code = :region_code
              AND cell.grid_version = :grid_version
              AND cell.geom && bounds.geom_4326
              AND ST_Intersects(cell.geom, bounds.geom_4326)
        )
        SELECT count(*) AS current_result_cells
        FROM tile_cells cell
        JOIN {verification_table} result
          ON result.grid_cell_id = cell.id
         AND result.region_id = cell.region_id
         AND result.is_current
         AND result.metadata->>'pipeline' = 'stage1_verification'
         AND result.region_code = :region_code
         AND result.grid_version = :grid_version
        """


def _raw_grid_mvt_sql(grid_table: str = "grid_cells") -> str:
    return f"""
        WITH bounds AS (
            SELECT
                ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        ),
        tile AS (
            SELECT
                cell.uuid::text AS cell_uuid,
                ST_AsMVTGeom(
                    ST_Transform(
                        ST_CollectionExtract(ST_MakeValid(cell.geom), 3),
                        3857
                    ),
                    bounds.geom_3857,
                    4096,
                    64,
                    true
                ) AS geom
            FROM {grid_table} cell
            JOIN regions region ON region.id = cell.region_id
            CROSS JOIN bounds
            WHERE region.code = :region_code
              AND cell.grid_version = :grid_version
              AND cell.geom && bounds.geom_4326
              AND ST_Intersects(cell.geom, bounds.geom_4326)
        )
        SELECT length(ST_AsMVT(tile, 'grid', 4096, 'geom')) AS tile_bytes
        FROM tile
    """


def _layer_b_mvt_sql(grid_table: str = "grid_cells") -> str:
    sentinel_table = sentinel_observations_table_sql()
    return f"""
        WITH bounds AS (
            SELECT
                ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        ),
        tile AS (
            SELECT
                cell.uuid::text AS cell_uuid,
                observation.ndvi,
                ST_AsMVTGeom(
                    ST_Transform(
                        ST_CollectionExtract(ST_MakeValid(cell.geom), 3),
                        3857
                    ),
                    bounds.geom_3857,
                    4096,
                    64,
                    true
                ) AS geom
            FROM {grid_table} cell
            JOIN regions region ON region.id = cell.region_id
            JOIN {sentinel_table} observation
              ON observation.grid_cell_id = cell.id
             AND observation.region_id = cell.region_id
             AND observation.is_current
             AND observation.metadata->>'pipeline' = 'stage1_cdse_ndvi'
             AND observation.region_code = :region_code
             AND observation.grid_version = :grid_version
            CROSS JOIN bounds
            WHERE region.code = :region_code
              AND cell.grid_version = :grid_version
              AND cell.geom && bounds.geom_4326
              AND ST_Intersects(cell.geom, bounds.geom_4326)
        )
        SELECT length(ST_AsMVT(tile, 'layer_b', 4096, 'geom')) AS tile_bytes
        FROM tile
    """


def _layer_c_mvt_sql(grid_table: str = "grid_cells") -> str:
    verification_table = verification_results_table_sql()
    return f"""
        WITH bounds AS (
            SELECT
                ST_TileEnvelope(:z, :x, :y) AS geom_3857,
                ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326) AS geom_4326
        ),
        tile AS (
            SELECT
                cell.uuid::text AS cell_uuid,
                result.anomaly_type,
                result.score,
                ST_AsMVTGeom(
                    ST_Transform(
                        ST_CollectionExtract(ST_MakeValid(cell.geom), 3),
                        3857
                    ),
                    bounds.geom_3857,
                    4096,
                    64,
                    true
                ) AS geom
            FROM {grid_table} cell
            JOIN regions region ON region.id = cell.region_id
            JOIN {verification_table} result
              ON result.grid_cell_id = cell.id
             AND result.region_id = cell.region_id
             AND result.is_current
             AND result.metadata->>'pipeline' = 'stage1_verification'
             AND result.region_code = :region_code
             AND result.grid_version = :grid_version
            CROSS JOIN bounds
            WHERE region.code = :region_code
              AND cell.grid_version = :grid_version
              AND cell.geom && bounds.geom_4326
              AND ST_Intersects(cell.geom, bounds.geom_4326)
        )
        SELECT length(ST_AsMVT(tile, 'layer_c', 4096, 'geom')) AS tile_bytes
        FROM tile
    """


def _carbon_asset_page_sql(grid_table: str = "grid_cells") -> str:
    return f"""
        WITH region_scope AS (
            SELECT id
            FROM regions
            WHERE code = :region_code
        )
        SELECT
            cell.uuid::text AS cell_uuid,
            assessment.status,
            assessment.total_potential_tco2e,
            assessment.coefficient_set_version,
            assessment.metadata->>'trend_evidence_state' AS trend_evidence_state
        FROM carbon_assessments assessment
        JOIN {grid_table} cell ON cell.id = assessment.grid_cell_id
        WHERE assessment.is_current
          AND assessment.metadata->>'pipeline' = 'stage3_carbon_assessment'
          AND cell.region_id = (
              SELECT id
              FROM region_scope
          )
          AND cell.grid_version = :grid_version
        ORDER BY assessment.created_at DESC, assessment.id DESC
        LIMIT :limit
    """


def _carbon_audit_tail_sql(grid_table: str = "grid_cells") -> str:
    return f"""
        WITH region_scope AS (
            SELECT id
            FROM regions
            WHERE code = :region_code
        )
        SELECT
            cell.uuid::text AS cell_uuid,
            event.event_type,
            event.created_at
        FROM carbon_assessment_events event
        JOIN {grid_table} cell ON cell.id = event.grid_cell_id
        WHERE event.metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
          AND cell.region_id = (
              SELECT id
              FROM region_scope
          )
          AND cell.grid_version = :grid_version
        ORDER BY event.created_at DESC, event.id DESC
        LIMIT :limit
    """


if __name__ == "__main__":
    main()
