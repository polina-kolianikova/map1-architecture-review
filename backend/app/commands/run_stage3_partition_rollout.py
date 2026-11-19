from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal


PARTITION_TABLES = (
    "grid_cells",
    "sentinel_observations",
    "verification_results",
)

REGION_SCOPED_CHILDREN = {
    "sentinel_observations": "grid_cells",
    "verification_results": "grid_cells",
}

PARTITION_INDEXES = {
    "grid_cells": (
        "CREATE INDEX IF NOT EXISTS grid_cells_partitioned_geom_gix ON grid_cells_partitioned USING GIST (geom)",
        "CREATE INDEX IF NOT EXISTS grid_cells_partitioned_region_version_uuid_idx ON grid_cells_partitioned (region_id, grid_version, uuid)",
        "CREATE INDEX IF NOT EXISTS grid_cells_partitioned_region_version_id_idx ON grid_cells_partitioned (region_id, grid_version, id)",
    ),
    "sentinel_observations": (
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_partitioned_scope_grid_cell_date_idx
        ON sentinel_observations_partitioned (region_id, grid_version, grid_cell_id, acquisition_date DESC, id DESC)
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
        """,
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_partitioned_stage4_scope_grid_cell_date_idx
        ON sentinel_observations_partitioned (region_id, grid_version, grid_cell_id, acquisition_date DESC, id DESC)
        WHERE metadata->>'pipeline' = 'stage4_layer_b_ndvi'
        """,
    ),
    "verification_results": (
        """
        CREATE INDEX IF NOT EXISTS verification_results_partitioned_scope_grid_cell_idx
        ON verification_results_partitioned (region_id, grid_version, grid_cell_id, created_at DESC, id DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """,
        """
        CREATE INDEX IF NOT EXISTS verification_results_partitioned_scope_status_idx
        ON verification_results_partitioned (region_id, grid_version, anomaly_type, created_at DESC, id DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """,
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Stage 3 region partition rollout plan")
    parser.add_argument("--region-code", action="append", default=[])
    parser.add_argument("--allow-online-shadow", action="store_true")
    parser.add_argument("--emit-sql", action="store_true")
    parser.add_argument("--apply-shadow", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=500_000)
    parser.add_argument("--output", default="")
    parser.add_argument("--fail-on-blockers", action="store_true")
    args = parser.parse_args()
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be greater than zero")

    with SessionLocal() as db:
        table_state = _load_table_state(db)
        regions = _load_regions(db, args.region_code)
        existing_partitions = _load_partition_status(db)
        applied = False
        if args.apply_shadow:
            preflight = build_partition_rollout_plan(
                table_state=table_state,
                regions=regions,
                existing_partitions=existing_partitions,
                allow_online_shadow=args.allow_online_shadow,
                emit_sql=True,
            )
            if preflight["blockers"]:
                raise SystemExit("Stage 3 partition rollout has blockers")
            _apply_shadow_rollout(db, regions, chunk_size=args.chunk_size)
            applied = True
            table_state = _load_table_state(db)
            existing_partitions = _load_partition_status(db)
        shadow_partitions = _load_shadow_partition_status(db)
        shadow_validation = _validate_shadow_partitions(db, regions)

    report = build_partition_rollout_plan(
        table_state=table_state,
        regions=regions,
        existing_partitions=existing_partitions,
        allow_online_shadow=args.allow_online_shadow,
        emit_sql=args.emit_sql,
    )
    report["applied_shadow"] = applied
    report["shadow_partitions"] = shadow_partitions
    report["shadow_validation"] = shadow_validation
    report["chunk_size"] = args.chunk_size
    payload = json.dumps(report, indent=2, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if args.fail_on_blockers and report["blockers"]:
        raise SystemExit("Stage 3 partition rollout has blockers")


def build_partition_rollout_plan(
    *,
    table_state: dict[str, dict[str, Any]],
    regions: list[dict[str, Any]],
    existing_partitions: dict[str, bool],
    allow_online_shadow: bool,
    emit_sql: bool,
) -> dict[str, Any]:
    blockers = _partition_blockers(
        table_state=table_state,
        regions=regions,
        existing_partitions=existing_partitions,
        allow_online_shadow=allow_online_shadow,
    )
    cutover_blockers = _cutover_blockers(existing_partitions)
    phases = [
        {
            "name": "prepare_shadow_tables",
            "goal": "create region-partitioned shadow tables without touching active production tables",
        },
        {
            "name": "create_region_partitions",
            "goal": "create one partition per active region plus a default partition",
        },
        {
            "name": "backfill_by_region",
            "goal": "copy rows in region batches and keep the source tables intact until validation passes",
        },
        {
            "name": "validate_counts_and_indexes",
            "goal": "compare row counts, rebuild query indexes, and run ANALYZE",
        },
        {
            "name": "cutover",
            "goal": "switch API-facing names or views during a maintenance window",
        },
    ]
    return {
        "stage": "stage3",
        "scope": "postgis_region_partitioning",
        "ready_to_apply": not blockers,
        "blockers": blockers,
        "tables": table_state,
        "regions": regions,
        "existing_partitions": existing_partitions,
        "cutover_ready": not cutover_blockers,
        "cutover_blockers": cutover_blockers,
        "cutover_policy": "shadow rollout is validated evidence only; active cutover requires a separate maintenance window",
        "phases": phases,
        "sql": build_partition_sql(regions) if emit_sql else [],
    }


def build_partition_sql(regions: list[dict[str, Any]]) -> list[str]:
    return [
        *build_partition_schema_sql(regions),
        *build_partition_backfill_sql(regions),
    ]


def build_partition_schema_sql(regions: list[dict[str, Any]]) -> list[str]:
    statements: list[str] = []
    for table_name in PARTITION_TABLES:
        partitioned = _partitioned_name(table_name)
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {partitioned} "
            f"(LIKE {table_name} INCLUDING DEFAULTS INCLUDING STORAGE INCLUDING COMMENTS) "
            "PARTITION BY LIST (region_id)"
        )
        for region in regions:
            suffix = _safe_suffix(region["code"])
            statements.append(
                f"CREATE TABLE IF NOT EXISTS {partitioned}_{suffix} "
                f"PARTITION OF {partitioned} FOR VALUES IN ({int(region['id'])})"
            )
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {partitioned}_default "
            f"PARTITION OF {partitioned} DEFAULT"
        )
        statements.extend(_normalize_sql(index) for index in PARTITION_INDEXES.get(table_name, ()))
        statements.append(f"ANALYZE {partitioned}")
    return statements


def build_partition_backfill_sql(regions: list[dict[str, Any]]) -> list[str]:
    statements: list[str] = []
    for table_name in PARTITION_TABLES:
        statements.append(f"TRUNCATE {_partitioned_name(table_name)}")
    for table_name in PARTITION_TABLES:
        statements.extend(_backfill_sql(table_name, regions))
        statements.append(f"ANALYZE {_partitioned_name(table_name)}")
    return statements


def _partition_blockers(
    *,
    table_state: dict[str, dict[str, Any]],
    regions: list[dict[str, Any]],
    existing_partitions: dict[str, bool],
    allow_online_shadow: bool,
) -> list[str]:
    blockers: list[str] = []
    if not regions:
        blockers.append("no regions selected for partition rollout")
    missing_tables = [name for name, state in table_state.items() if not state["exists"]]
    if missing_tables:
        blockers.append(f"missing tables: {', '.join(missing_tables)}")
    missing_region_columns = [
        name
        for name, state in table_state.items()
        if state["exists"] and not state["has_region_id"]
    ]
    if missing_region_columns:
        blockers.append(f"missing region_id columns: {', '.join(missing_region_columns)}")
    active_unpartitioned = [
        name
        for name, state in table_state.items()
        if state["exists"] and _table_row_count(state) > 0 and not existing_partitions.get(name, False)
    ]
    if active_unpartitioned and not allow_online_shadow:
        blockers.append(
            "non-empty FK-bound tables require shadow rollout: "
            + ", ".join(active_unpartitioned)
        )
    return blockers


def _cutover_blockers(existing_partitions: dict[str, bool]) -> list[str]:
    missing = [name for name in PARTITION_TABLES if not existing_partitions.get(name, False)]
    if not missing:
        return []
    return [
        "active tables are not region-partitioned: " + ", ".join(missing),
        "active cutover must validate constraints, indexes, foreign-key paths, API reads, and rollback before table/view switching",
    ]


def _backfill_sql(table_name: str, regions: list[dict[str, Any]]) -> list[str]:
    partitioned = _partitioned_name(table_name)
    if table_name not in REGION_SCOPED_CHILDREN:
        return [
            f"INSERT INTO {partitioned} SELECT * FROM {table_name} WHERE region_id = {int(region['id'])}"
            for region in regions
        ]
    return [
        (
            f"INSERT INTO {partitioned} "
            f"SELECT source.* FROM {table_name} source "
            "JOIN grid_cells cell ON cell.id = source.grid_cell_id "
            f"WHERE cell.region_id = {int(region['id'])}"
        )
        for region in regions
    ]


def _load_table_state(db) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for table_name in PARTITION_TABLES:
        exists = _table_exists(db, table_name)
        result[table_name] = {
            "exists": exists,
            "has_region_id": _has_column(db, table_name, "region_id") if exists else False,
            "estimated_rows": _estimated_rows(db, table_name) if exists else 0,
            "row_count": _row_count(db, table_name) if exists else 0,
        }
    return result


def _load_regions(db, region_codes: list[str]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    where = "WHERE true"
    if region_codes:
        params["region_codes"] = region_codes
        where = "WHERE code = ANY(:region_codes)"
    rows = db.execute(
        text(
            f"""
            SELECT id, code, name, country_code
            FROM regions
            {where}
            ORDER BY code
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def _load_partition_status(db) -> dict[str, bool]:
    rows = db.execute(
        text(
            """
            SELECT
                cls.relname,
                lower(pg_get_partkeydef(cls.oid)) AS partition_key
            FROM pg_partitioned_table partitioned
            JOIN pg_class cls ON cls.oid = partitioned.partrelid
            JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
            WHERE namespace.nspname = 'public'
              AND cls.relname = ANY(:names)
            """
        ),
        {"names": list(PARTITION_TABLES)},
    ).mappings().all()
    return {
        name: any(row["relname"] == name and "region" in (row["partition_key"] or "") for row in rows)
        for name in PARTITION_TABLES
    }


def _load_shadow_partition_status(db) -> dict[str, bool]:
    partitioned_names = [_partitioned_name(name) for name in PARTITION_TABLES]
    rows = db.execute(
        text(
            """
            SELECT
                cls.relname,
                lower(pg_get_partkeydef(cls.oid)) AS partition_key
            FROM pg_partitioned_table partitioned
            JOIN pg_class cls ON cls.oid = partitioned.partrelid
            JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
            WHERE namespace.nspname = 'public'
              AND cls.relname = ANY(:names)
            """
        ),
        {"names": partitioned_names},
    ).mappings().all()
    return {
        name: any(
            row["relname"] == _partitioned_name(name)
            and "region" in (row["partition_key"] or "")
            for row in rows
        )
        for name in PARTITION_TABLES
    }


def _validate_shadow_partitions(db, regions: list[dict[str, Any]]) -> dict[str, dict[str, int | bool]]:
    validation: dict[str, dict[str, int | bool]] = {}
    region_ids = [int(region["id"]) for region in regions]
    for table_name in PARTITION_TABLES:
        partitioned = _partitioned_name(table_name)
        source_exists = _table_exists(db, table_name)
        shadow_exists = _table_exists(db, partitioned)
        source_count = _scoped_row_count(db, table_name, region_ids) if source_exists else 0
        shadow_count = _safe_scoped_row_count(db, partitioned, region_ids) if shadow_exists else 0
        validation[table_name] = {
            "source_rows": source_count,
            "shadow_rows": shadow_count,
            "counts_match": source_exists and shadow_exists and source_count == shadow_count,
        }
    return validation


def _table_exists(db, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _has_column(db, table_name: str, column_name: str) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                      AND column_name = :column_name
                )
                """
            ),
            {"table_name": table_name, "column_name": column_name},
        ).scalar_one()
    )


def _estimated_rows(db, table_name: str) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT COALESCE(n_live_tup, 0)::bigint
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                  AND relname = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalar_one_or_none()
        or 0
    )


def _row_count(db, table_name: str) -> int:
    if table_name not in PARTITION_TABLES:
        raise ValueError(f"unsupported table: {table_name}")
    return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _scoped_row_count(db, table_name: str, region_ids: list[int]) -> int:
    if not region_ids:
        return _row_count(db, table_name)
    if table_name not in PARTITION_TABLES:
        raise ValueError(f"unsupported table: {table_name}")
    if table_name not in REGION_SCOPED_CHILDREN:
        return int(
            db.execute(
                text(f"SELECT count(*) FROM {table_name} WHERE region_id = ANY(:region_ids)"),
                {"region_ids": region_ids},
            ).scalar_one()
        )
    return int(
        db.execute(
            text(
                f"""
                SELECT count(*)
                FROM {table_name} source
                JOIN grid_cells cell ON cell.id = source.grid_cell_id
                WHERE cell.region_id = ANY(:region_ids)
                """
            ),
            {"region_ids": region_ids},
        ).scalar_one()
    )


def _safe_row_count(db, table_name: str) -> int:
    if not _identifier_is_safe(table_name):
        raise ValueError(f"unsupported table: {table_name}")
    return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _safe_scoped_row_count(db, table_name: str, region_ids: list[int]) -> int:
    if not region_ids:
        return _safe_row_count(db, table_name)
    if not _identifier_is_safe(table_name):
        raise ValueError(f"unsupported table: {table_name}")
    return int(
        db.execute(
            text(f"SELECT count(*) FROM {table_name} WHERE region_id = ANY(:region_ids)"),
            {"region_ids": region_ids},
        ).scalar_one()
    )


def _apply_shadow_rollout(db, regions: list[dict[str, Any]], *, chunk_size: int) -> None:
    _apply_sql(db, build_partition_schema_sql(regions))
    db.commit()
    for table_name in PARTITION_TABLES:
        db.execute(text(f"TRUNCATE {_partitioned_name(table_name)}"))
    db.commit()
    for table_name in PARTITION_TABLES:
        if table_name == "grid_cells":
            _backfill_grid_cells_in_chunks(db, regions, chunk_size=chunk_size)
        else:
            _apply_sql(db, _backfill_sql(table_name, regions))
        db.execute(text(f"ANALYZE {_partitioned_name(table_name)}"))
        db.commit()


def _backfill_grid_cells_in_chunks(db, regions: list[dict[str, Any]], *, chunk_size: int) -> None:
    partitioned = _partitioned_name("grid_cells")
    for region in regions:
        bounds = db.execute(
            text(
                """
                SELECT min(id) AS min_id, max(id) AS max_id
                FROM grid_cells
                WHERE region_id = :region_id
                """
            ),
            {"region_id": int(region["id"])},
        ).mappings().one()
        min_id = bounds["min_id"]
        max_id = bounds["max_id"]
        if min_id is None or max_id is None:
            continue
        start = int(min_id)
        max_value = int(max_id)
        while start <= max_value:
            end = min(start + chunk_size - 1, max_value)
            db.execute(
                text(
                    f"""
                    INSERT INTO {partitioned}
                    SELECT *
                    FROM grid_cells
                    WHERE region_id = :region_id
                      AND id BETWEEN :start_id AND :end_id
                    """
                ),
                {
                    "region_id": int(region["id"]),
                    "start_id": start,
                    "end_id": end,
                },
            )
            db.commit()
            start = end + 1


def _apply_sql(db, statements: list[str]) -> None:
    for statement in statements:
        db.execute(text(statement))


def _identifier_is_safe(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z_][a-z0-9_]*", value))


def _table_row_count(state: dict[str, Any]) -> int:
    if "row_count" in state:
        return int(state["row_count"] or 0)
    return int(state.get("estimated_rows") or 0)


def _partitioned_name(table_name: str) -> str:
    return f"{table_name}_partitioned"


def _safe_suffix(value: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return suffix or "region"


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.split())


if __name__ == "__main__":
    main()
