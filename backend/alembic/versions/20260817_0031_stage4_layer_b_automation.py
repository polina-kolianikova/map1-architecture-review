"""Add Stage 4 Layer B automation schema.

Revision ID: 20260817_0031
Revises: 20260812_0030
Create Date: 2026-08-17
"""

from collections.abc import Sequence
import os

from alembic import op


revision: str = "20260817_0031"
down_revision: str | None = "20260812_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STAGE4_TABLES = (
    "stage4_layer_b_runs",
    "stage4_layer_b_batches",
    "stage4_layer_b_events",
    "stage4_layer_b_metrics",
    "stage4_satellite_artifacts",
    "stage4_process_heartbeats",
    "stage4_alerts",
)


def upgrade() -> None:
    _widen_area_ha("grid_cells")
    _add_area_coefficient("grid_cells")
    _add_area_coefficient("grid_cells_partitioned")
    _create_area_coefficient_trigger()
    _attach_area_coefficient_trigger("grid_cells")
    _attach_area_coefficient_trigger("grid_cells_partitioned")
    _create_stage4_tables()
    _create_stage4_indexes()
    _create_stage4_observation_indexes()
    _grant_runtime_access()


def downgrade() -> None:
    _revoke_runtime_access()
    op.execute("DROP INDEX IF EXISTS sentinel_observations_partitioned_stage4_scope_grid_cell_date_idx")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_current_stage4_cell_uq")
    op.execute("DROP INDEX IF EXISTS sentinel_observations_stage4_scope_grid_cell_date_idx")
    for table_name in reversed(STAGE4_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table_name}")
    _drop_area_coefficient_trigger("grid_cells_partitioned")
    _drop_area_coefficient_trigger("grid_cells")
    op.execute("DROP FUNCTION IF EXISTS set_grid_cell_area_coefficient()")
    _drop_area_coefficient("grid_cells_partitioned")
    _drop_area_coefficient("grid_cells")


def _widen_area_ha(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NOT NULL
               AND EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = '{table_name}'
                      AND column_name = 'area_ha'
                      AND (
                          numeric_precision IS DISTINCT FROM 14
                          OR numeric_scale IS DISTINCT FROM 6
                      )
               ) THEN
                ALTER TABLE {table_name}
                ALTER COLUMN area_ha TYPE NUMERIC(14, 6);
            END IF;
        END $$;
        """
    )


def _add_area_coefficient(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NOT NULL THEN
                ALTER TABLE {table_name}
                ADD COLUMN IF NOT EXISTS area_coefficient NUMERIC(14, 6);

                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = '{table_name}_area_coefficient_positive'
                ) THEN
                    ALTER TABLE {table_name}
                    ADD CONSTRAINT {table_name}_area_coefficient_positive
                    CHECK (area_coefficient IS NULL OR area_coefficient > 0)
                    NOT VALID;
                END IF;
            END IF;
        END $$;
        """
    )


def _drop_area_coefficient(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NOT NULL THEN
                ALTER TABLE {table_name}
                DROP CONSTRAINT IF EXISTS {table_name}_area_coefficient_positive;
                ALTER TABLE {table_name}
                DROP COLUMN IF EXISTS area_coefficient;
            END IF;
        END $$;
        """
    )


def _create_area_coefficient_trigger() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_grid_cell_area_coefficient()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.area_coefficient IS NULL THEN
                NEW.area_coefficient := ROUND(NEW.area_ha::numeric, 6);
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )


def _attach_area_coefficient_trigger(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NOT NULL
               AND EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = '{table_name}'
                      AND column_name = 'area_coefficient'
               )
               AND NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = '{table_name}_set_area_coefficient'
               ) THEN
                CREATE TRIGGER {table_name}_set_area_coefficient
                BEFORE INSERT OR UPDATE OF area_ha, area_coefficient ON {table_name}
                FOR EACH ROW
                EXECUTE FUNCTION set_grid_cell_area_coefficient();
            END IF;
        END $$;
        """
    )


def _drop_area_coefficient_trigger(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NOT NULL THEN
                DROP TRIGGER IF EXISTS {table_name}_set_area_coefficient ON {table_name};
            END IF;
        END $$;
        """
    )


def _create_stage4_tables() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_layer_b_runs (
            id BIGSERIAL PRIMARY KEY,
            run_uuid UUID NOT NULL DEFAULT gen_random_uuid(),
            region_id BIGINT REFERENCES regions(id) ON DELETE SET NULL,
            region_code TEXT NOT NULL,
            grid_version TEXT NOT NULL,
            trigger_source TEXT NOT NULL DEFAULT 'manual',
            cadence_days INTEGER NOT NULL,
            requested_start DATE NOT NULL,
            requested_end DATE NOT NULL,
            max_cloud_coverage INTEGER NOT NULL,
            min_valid_pixel_ratio NUMERIC(6, 5) NOT NULL,
            resolution NUMERIC(12, 8) NOT NULL,
            batch_size INTEGER NOT NULL,
            total_cells BIGINT NOT NULL DEFAULT 0,
            total_batches INTEGER NOT NULL DEFAULT 0,
            queued_batches INTEGER NOT NULL DEFAULT 0,
            processed_cells BIGINT NOT NULL DEFAULT 0,
            succeeded_cells BIGINT NOT NULL DEFAULT 0,
            failed_cells BIGINT NOT NULL DEFAULT 0,
            skipped_cells BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned',
            latest_error TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage4_layer_b_runs_uuid_uq UNIQUE (run_uuid),
            CONSTRAINT stage4_layer_b_runs_status_valid CHECK (
                status IN (
                    'planned',
                    'queued',
                    'running',
                    'external_wait',
                    'external_ready',
                    'completed',
                    'completed_with_errors',
                    'failed',
                    'cancelled',
                    'stuck'
                )
            ),
            CONSTRAINT stage4_layer_b_runs_cadence_valid CHECK (cadence_days BETWEEN 5 AND 10),
            CONSTRAINT stage4_layer_b_runs_cloud_valid CHECK (max_cloud_coverage BETWEEN 0 AND 100),
            CONSTRAINT stage4_layer_b_runs_ratio_valid CHECK (
                min_valid_pixel_ratio >= 0 AND min_valid_pixel_ratio <= 1
            ),
            CONSTRAINT stage4_layer_b_runs_batch_size_positive CHECK (batch_size > 0),
            CONSTRAINT stage4_layer_b_runs_period_valid CHECK (requested_start <= requested_end)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_layer_b_batches (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES stage4_layer_b_runs(id) ON DELETE CASCADE,
            task_name TEXT NOT NULL,
            batch_index INTEGER NOT NULL,
            grid_cell_id_start BIGINT NOT NULL,
            grid_cell_id_end BIGINT NOT NULL,
            expected_cells INTEGER NOT NULL DEFAULT 0,
            processed_cells INTEGER NOT NULL DEFAULT 0,
            succeeded_cells INTEGER NOT NULL DEFAULT 0,
            failed_cells INTEGER NOT NULL DEFAULT 0,
            skipped_cells INTEGER NOT NULL DEFAULT 0,
            celery_task_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            latest_error TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            locked_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage4_layer_b_batches_run_task_index_uq UNIQUE (
                run_id,
                task_name,
                batch_index
            ),
            CONSTRAINT stage4_layer_b_batches_task_valid CHECK (
                task_name IN ('ndvi_update', 'biomass_update')
            ),
            CONSTRAINT stage4_layer_b_batches_status_valid CHECK (
                status IN (
                    'queued',
                    'running',
                    'external_wait',
                    'external_ready',
                    'completed',
                    'completed_with_errors',
                    'failed',
                    'cancelled',
                    'skipped',
                    'stuck'
                )
            ),
            CONSTRAINT stage4_layer_b_batches_range_valid CHECK (
                grid_cell_id_start <= grid_cell_id_end
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_layer_b_metrics (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT REFERENCES stage4_layer_b_runs(id) ON DELETE SET NULL,
            batch_id BIGINT REFERENCES stage4_layer_b_batches(id) ON DELETE SET NULL,
            grid_cell_id BIGINT NOT NULL REFERENCES grid_cells(id) ON DELETE CASCADE,
            region_id BIGINT REFERENCES regions(id) ON DELETE SET NULL,
            region_code TEXT NOT NULL,
            grid_version TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_date DATE NOT NULL,
            value NUMERIC(14, 6),
            per_hectare_value NUMERIC(14, 6),
            area_coefficient NUMERIC(14, 6) NOT NULL,
            area_weighted_value NUMERIC(18, 6),
            unit TEXT NOT NULL,
            source_status TEXT NOT NULL,
            source_provider TEXT NOT NULL,
            source_product_id TEXT NOT NULL DEFAULT 'none',
            quality_score NUMERIC(8, 5),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage4_layer_b_metrics_unique_signal_uq UNIQUE (
                grid_cell_id,
                metric_name,
                metric_date,
                source_provider,
                source_product_id
            ),
            CONSTRAINT stage4_layer_b_metrics_name_valid CHECK (
                metric_name IN ('ndvi', 'biomass')
            ),
            CONSTRAINT stage4_layer_b_metrics_area_positive CHECK (area_coefficient > 0),
            CONSTRAINT stage4_layer_b_metrics_quality_range CHECK (
                quality_score IS NULL OR quality_score BETWEEN 0 AND 1
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_layer_b_events (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT REFERENCES stage4_layer_b_runs(id) ON DELETE CASCADE,
            batch_id BIGINT REFERENCES stage4_layer_b_batches(id) ON DELETE SET NULL,
            severity TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage4_layer_b_events_severity_valid CHECK (
                severity IN ('info', 'warning', 'critical')
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_satellite_artifacts (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT REFERENCES stage4_layer_b_runs(id) ON DELETE SET NULL,
            batch_id BIGINT REFERENCES stage4_layer_b_batches(id) ON DELETE SET NULL,
            region_id BIGINT REFERENCES regions(id) ON DELETE SET NULL,
            region_code TEXT NOT NULL,
            grid_version TEXT NOT NULL,
            provider TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            acquisition_date DATE,
            storage_uri TEXT NOT NULL,
            checksum TEXT,
            size_bytes BIGINT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage4_satellite_artifacts_storage_uq UNIQUE (
                provider,
                artifact_type,
                acquisition_date,
                storage_uri
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_process_heartbeats (
            id BIGSERIAL PRIMARY KEY,
            service_name TEXT NOT NULL,
            worker_name TEXT NOT NULL,
            pid INTEGER,
            run_id BIGINT REFERENCES stage4_layer_b_runs(id) ON DELETE SET NULL,
            batch_id BIGINT REFERENCES stage4_layer_b_batches(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT stage4_process_heartbeats_worker_uq UNIQUE (service_name, worker_name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_alerts (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT REFERENCES stage4_layer_b_runs(id) ON DELETE SET NULL,
            batch_id BIGINT REFERENCES stage4_layer_b_batches(id) ON DELETE SET NULL,
            severity TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            message TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ,
            CONSTRAINT stage4_alerts_status_valid CHECK (
                status IN ('open', 'acknowledged', 'resolved')
            ),
            CONSTRAINT stage4_alerts_severity_valid CHECK (
                severity IN ('info', 'warning', 'critical')
            )
        )
        """
    )


def _create_stage4_indexes() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_runs_scope_status_idx
        ON stage4_layer_b_runs (region_code, grid_version, status, created_at DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_batches_status_heartbeat_idx
        ON stage4_layer_b_batches (status, heartbeat_at, updated_at, id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_batches_run_task_status_idx
        ON stage4_layer_b_batches (run_id, task_name, status, batch_index)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_metrics_scope_metric_date_idx
        ON stage4_layer_b_metrics (
            region_id,
            grid_version,
            metric_name,
            metric_date DESC,
            grid_cell_id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_metrics_cell_metric_date_idx
        ON stage4_layer_b_metrics (grid_cell_id, metric_name, metric_date DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_events_run_created_idx
        ON stage4_layer_b_events (run_id, created_at DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_layer_b_events_batch_created_idx
        ON stage4_layer_b_events (batch_id, created_at DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_satellite_artifacts_scope_date_idx
        ON stage4_satellite_artifacts (
            region_code,
            grid_version,
            provider,
            artifact_type,
            acquisition_date DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_process_heartbeats_at_idx
        ON stage4_process_heartbeats (heartbeat_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage4_alerts_open_scope_idx
        ON stage4_alerts (status, severity, last_seen_at DESC, id DESC)
        WHERE status <> 'resolved'
        """
    )


def _create_stage4_observation_indexes() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS sentinel_observations_current_stage4_cell_uq
        ON sentinel_observations (
            grid_cell_id,
            (metadata->>'pipeline'),
            region_code,
            grid_version
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage4_layer_b_ndvi'
          AND region_code IS NOT NULL
          AND grid_version IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_stage4_scope_grid_cell_date_idx
        ON sentinel_observations (region_id, grid_version, grid_cell_id, acquisition_date DESC, id DESC)
        WHERE metadata->>'pipeline' = 'stage4_layer_b_ndvi'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.sentinel_observations_partitioned') IS NOT NULL THEN
                CREATE INDEX IF NOT EXISTS sentinel_observations_partitioned_stage4_scope_grid_cell_date_idx
                ON sentinel_observations_partitioned (
                    region_id,
                    grid_version,
                    grid_cell_id,
                    acquisition_date DESC,
                    id DESC
                )
                WHERE metadata->>'pipeline' = 'stage4_layer_b_ndvi';
            END IF;
        END $$;
        """
    )


def _grant_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    stage4_tables = ", ".join(STAGE4_TABLES)
    stage4_sequences = ", ".join(f"{table_name}_id_seq" for table_name in STAGE4_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE {stage4_tables} TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE {stage4_sequences} TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT SELECT, UPDATE (area_coefficient) ON TABLE grid_cells TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT SELECT, INSERT ON TABLE sentinel_observations TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT UPDATE (is_current) ON TABLE sentinel_observations TO {_quote_identifier(app_role)}';
                IF to_regclass('public.grid_cells_partitioned') IS NOT NULL THEN
                    EXECUTE 'GRANT SELECT, UPDATE (area_coefficient) ON TABLE grid_cells_partitioned TO {_quote_identifier(app_role)}';
                END IF;
                IF to_regclass('public.sentinel_observations_partitioned') IS NOT NULL THEN
                    EXECUTE 'GRANT SELECT, INSERT ON TABLE sentinel_observations_partitioned TO {_quote_identifier(app_role)}';
                    EXECUTE 'GRANT UPDATE (is_current) ON TABLE sentinel_observations_partitioned TO {_quote_identifier(app_role)}';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE {stage4_tables} TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _revoke_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    stage4_tables = ", ".join(STAGE4_TABLES)
    stage4_sequences = ", ".join(f"{table_name}_id_seq" for table_name in STAGE4_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'REVOKE SELECT, INSERT, UPDATE ON TABLE {stage4_tables} FROM {_quote_identifier(app_role)}';
                EXECUTE 'REVOKE USAGE, SELECT ON SEQUENCE {stage4_sequences} FROM {_quote_identifier(app_role)}';
                IF to_regclass('public.sentinel_observations_partitioned') IS NOT NULL THEN
                    EXECUTE 'REVOKE INSERT ON TABLE sentinel_observations_partitioned FROM {_quote_identifier(app_role)}';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'REVOKE SELECT ON TABLE {stage4_tables} FROM {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
