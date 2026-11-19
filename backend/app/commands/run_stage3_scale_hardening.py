from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db.session import SessionLocal


INDEX_DDL = {
    "regions_geom_gix": "CREATE INDEX IF NOT EXISTS regions_geom_gix ON regions USING GIST (geom)",
    "grid_cells_geom_gix": "CREATE INDEX IF NOT EXISTS grid_cells_geom_gix ON grid_cells USING GIST (geom)",
    "ogm_parcels_geom_gix": "CREATE INDEX IF NOT EXISTS ogm_parcels_geom_gix ON ogm_parcels USING GIST (geom)",
    "grid_cells_region_version_uuid_idx": """
        CREATE INDEX IF NOT EXISTS grid_cells_region_version_uuid_idx
        ON grid_cells (region_id, grid_version, uuid)
    """,
    "grid_cells_region_version_id_idx": """
        CREATE INDEX IF NOT EXISTS grid_cells_region_version_id_idx
        ON grid_cells (region_id, grid_version, id) INCLUDE (uuid)
    """,
    "ogm_parcels_current_region_layer_idx": """
        CREATE INDEX IF NOT EXISTS ogm_parcels_current_region_layer_idx
        ON ogm_parcels ((attributes->>'region_code'), source_layer, fetched_at DESC)
        WHERE is_current
    """,
    "sentinel_observations_stage_region_grid_cell_date_idx": """
        CREATE INDEX IF NOT EXISTS sentinel_observations_stage_region_grid_cell_date_idx
        ON sentinel_observations (
            (metadata->>'region_code'),
            (metadata->>'grid_version'),
            grid_cell_id,
            acquisition_date DESC,
            id DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
    """,
    "sentinel_observations_scope_grid_cell_date_idx": """
        CREATE INDEX IF NOT EXISTS sentinel_observations_scope_grid_cell_date_idx
        ON sentinel_observations (
            region_id,
            grid_version,
            grid_cell_id,
            acquisition_date DESC,
            id DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_cdse_ndvi'
    """,
    "verification_results_stage_region_grid_cell_idx": """
        CREATE INDEX IF NOT EXISTS verification_results_stage_region_grid_cell_idx
        ON verification_results (
            (metadata->>'region_code'),
            (metadata->>'grid_version'),
            grid_cell_id,
            created_at DESC,
            id DESC
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
    """,
    "verification_results_scope_grid_cell_idx": """
        CREATE INDEX IF NOT EXISTS verification_results_scope_grid_cell_idx
        ON verification_results (
            region_id,
            grid_version,
            grid_cell_id,
            created_at DESC,
            id DESC
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
    """,
    "verification_results_scope_status_idx": """
        CREATE INDEX IF NOT EXISTS verification_results_scope_status_idx
        ON verification_results (
            region_id,
            grid_version,
            anomaly_type,
            created_at DESC,
            id DESC
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
    """,
    "verification_audit_events_stage_region_grid_cell_idx": """
        CREATE INDEX IF NOT EXISTS verification_audit_events_stage_region_grid_cell_idx
        ON verification_audit_events (
            (metadata->>'region_code'),
            (metadata->'latest_result_metadata'->>'grid_version'),
            grid_cell_id,
            created_at DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_verification_audit'
    """,
    "verification_audit_events_scope_cell_created_idx": """
        CREATE INDEX IF NOT EXISTS verification_audit_events_scope_cell_created_idx
        ON verification_audit_events (
            region_id,
            grid_version,
            grid_cell_id,
            created_at DESC
        )
        WHERE metadata->>'pipeline' = 'stage1_verification_audit'
    """,
    "carbon_assessments_current_created_cell_idx": """
        CREATE INDEX IF NOT EXISTS carbon_assessments_current_created_cell_idx
        ON carbon_assessments (created_at DESC, id DESC, grid_cell_id)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage3_carbon_assessment'
    """,
    "carbon_assessment_events_pipeline_created_cell_idx": """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_pipeline_created_cell_idx
        ON carbon_assessment_events (created_at DESC, id DESC, grid_cell_id)
        WHERE metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
    """,
    "carbon_assessment_events_audit_assessment_cell_idx": """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_audit_assessment_cell_idx
        ON carbon_assessment_events (carbon_assessment_id, grid_cell_id)
        WHERE metadata->>'pipeline' = 'stage3_carbon_assessment_audit'
    """,
}

ANALYZE_TABLES = (
    "regions",
    "grid_cells",
    "ogm_parcels",
    "sentinel_observations",
    "verification_results",
    "verification_audit_events",
    "carbon_assessments",
    "carbon_assessment_events",
)

PARTITION_TARGETS = (
    "grid_cells",
    "sentinel_observations",
    "verification_results",
)

REGION_SCOPED_CHILDREN = {
    "sentinel_observations": "grid_cells",
    "verification_results": "grid_cells",
}

SCOPE_COLUMNS = {
    "sentinel_observations": ("region_id", "region_code", "grid_version"),
    "verification_results": ("region_id", "region_code", "grid_version"),
    "verification_audit_events": ("region_id", "region_code", "grid_version"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply and verify Stage 3 scale hardening")
    parser.add_argument("--region-code", default="")
    parser.add_argument("--apply-indexes", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--acceptance-scope", choices=("public-ogm", "full-scale"), default="full-scale")
    parser.add_argument("--require-partitions", action="store_true")
    parser.add_argument("--allow-shadow-partitions", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    require_partitions = args.require_partitions or args.acceptance_scope == "full-scale"
    allow_shadow_partitions = args.allow_shadow_partitions or args.acceptance_scope == "public-ogm"

    with SessionLocal() as db:
        if args.apply_indexes:
            _require_owner_role(db, "--apply-indexes")
            _apply_indexes(db)
            db.commit()
        if args.analyze:
            _analyze_tables(db)
            db.commit()
        report = {
            "stage": "stage3",
            "indexes": _index_status(db),
            "scope_columns": _scope_column_status(db),
            "partitions": _partition_status(db),
            "shadow_partitions": _shadow_partition_status(db),
            "shadow_partition_counts_match": _shadow_partition_validation(db, args.region_code),
            "table_stats": _table_stats(db),
        }
    report["partition_policy"] = {
        "require_partitions": require_partitions,
        "allow_shadow_partitions": allow_shadow_partitions,
        "acceptance_scope": args.acceptance_scope,
    }
    partition_ready = all(
        _partition_ready(report, name, allow_shadow=allow_shadow_partitions)
        for name in PARTITION_TARGETS
    )
    report["ready"] = all(report["indexes"].values()) and all(report["scope_columns"].values()) and (
        partition_ready or not require_partitions
    )
    report["blockers"] = _blockers(
        report,
        require_partitions=require_partitions,
        allow_shadow_partitions=allow_shadow_partitions,
    )
    payload = json.dumps(report, indent=2, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if report["blockers"]:
        raise SystemExit("Stage 3 scale hardening has blockers")


def _apply_indexes(db) -> None:
    status = _index_status(db)
    for name, ddl in INDEX_DDL.items():
        if status.get(name):
            continue
        db.execute(text(ddl))


def _require_owner_role(db, option_name: str) -> None:
    allowed = bool(
        db.execute(
            text(
                """
                SELECT pg_has_role(current_user, tableowner, 'MEMBER')
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename = 'grid_cells'
                """
            )
        ).scalar_one_or_none()
    )
    if not allowed:
        raise SystemExit(f"{option_name} requires the database owner role")


def _analyze_tables(db) -> None:
    for table in ANALYZE_TABLES:
        if _table_exists(db, table):
            db.execute(text(f"ANALYZE {table}"))


def _index_status(db) -> dict[str, bool]:
    rows = db.execute(
        text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = ANY(:names)
            """
        ),
        {"names": list(INDEX_DDL)},
    ).scalars().all()
    return {name: name in rows for name in INDEX_DDL}


def _partition_status(db) -> dict[str, bool]:
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
        {"names": list(PARTITION_TARGETS)},
    ).mappings().all()
    return {
        name: any(row["relname"] == name and "region" in (row["partition_key"] or "") for row in rows)
        for name in PARTITION_TARGETS
    }


def _shadow_partition_status(db) -> dict[str, bool]:
    names = [f"{name}_partitioned" for name in PARTITION_TARGETS]
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
        {"names": names},
    ).mappings().all()
    return {
        name: any(row["relname"] == f"{name}_partitioned" and "region" in (row["partition_key"] or "") for row in rows)
        for name in PARTITION_TARGETS
    }


def _shadow_partition_validation(db, region_code: str = "") -> dict[str, bool]:
    validation: dict[str, bool] = {}
    region_ids = _region_ids(db, region_code)
    for table_name in PARTITION_TARGETS:
        shadow_name = f"{table_name}_partitioned"
        if not _table_exists(db, table_name) or not _table_exists(db, shadow_name):
            validation[table_name] = False
            continue
        validation[table_name] = _scoped_row_count(db, table_name, region_ids) == _count_known_shadow_table(
            db,
            shadow_name,
            region_ids,
        )
    return validation


def _region_ids(db, region_code: str) -> list[int]:
    if not region_code:
        return []
    rows = db.execute(
        text("SELECT id FROM regions WHERE code = :region_code"),
        {"region_code": region_code},
    ).scalars().all()
    return [int(row) for row in rows]


def _scope_column_status(db) -> dict[str, bool]:
    rows = db.execute(
        text(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(:table_names)
            """
        ),
        {"table_names": list(SCOPE_COLUMNS)},
    ).mappings().all()
    present = {(row["table_name"], row["column_name"]) for row in rows}
    return {
        table_name: all((table_name, column_name) in present for column_name in columns)
        for table_name, columns in SCOPE_COLUMNS.items()
    }


def _table_stats(db) -> dict[str, dict[str, int]]:
    rows = db.execute(
        text(
            """
            SELECT
                relname AS table_name,
                COALESCE(n_live_tup, 0)::bigint AS estimated_rows,
                COALESCE(n_dead_tup, 0)::bigint AS estimated_dead_rows
            FROM pg_stat_user_tables
            WHERE schemaname = 'public'
              AND relname = ANY(:names)
            ORDER BY relname
            """
        ),
        {"names": list(ANALYZE_TABLES)},
    ).mappings().all()
    return {
        row["table_name"]: {
            "estimated_rows": int(row["estimated_rows"]),
            "estimated_dead_rows": int(row["estimated_dead_rows"]),
            "row_count": _row_count(db, row["table_name"]),
        }
        for row in rows
    }


def _row_count(db, table_name: str) -> int:
    if table_name not in ANALYZE_TABLES:
        raise ValueError(f"unsupported table: {table_name}")
    return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _scoped_row_count(db, table_name: str, region_ids: list[int]) -> int:
    if not region_ids:
        return _row_count(db, table_name)
    if table_name not in PARTITION_TARGETS:
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


def _blockers(report: dict, *, require_partitions: bool, allow_shadow_partitions: bool = False) -> list[str]:
    blockers = []
    missing_indexes = [name for name, ready in report["indexes"].items() if not ready]
    if missing_indexes:
        blockers.append(f"missing indexes: {', '.join(missing_indexes)}")
    missing_scope_columns = [name for name, ready in report.get("scope_columns", {}).items() if not ready]
    if missing_scope_columns:
        blockers.append(f"missing scale scope columns: {', '.join(missing_scope_columns)}")
    if require_partitions:
        missing_partitions = [
            name
            for name in PARTITION_TARGETS
            if not _partition_ready(report, name, allow_shadow=allow_shadow_partitions)
        ]
        if missing_partitions:
            partition_label = "validated region partitions" if allow_shadow_partitions else "region partitions"
            blockers.append(
                f"missing {partition_label}: "
                f"{', '.join(missing_partitions)}; "
                "generate the rollout with run_stage3_partition_rollout"
            )
    return blockers


def _partition_ready(report: dict, table_name: str, *, allow_shadow: bool) -> bool:
    if report["partitions"].get(table_name):
        return True
    if not allow_shadow:
        return False
    return bool(
        report.get("shadow_partitions", {}).get(table_name)
        and report.get("shadow_partition_counts_match", {}).get(table_name)
    )


def _table_exists(db, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT to_regclass('public.' || quote_ident(:table_name)) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _count_known_shadow_table(db, table_name: str, region_ids: list[int] | None = None) -> int:
    if table_name not in {f"{name}_partitioned" for name in PARTITION_TARGETS}:
        raise ValueError(f"unsupported table: {table_name}")
    if region_ids:
        return int(
            db.execute(
                text(f"SELECT count(*) FROM {table_name} WHERE region_id = ANY(:region_ids)"),
                {"region_ids": region_ids},
            ).scalar_one()
        )
    return int(db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


if __name__ == "__main__":
    main()
