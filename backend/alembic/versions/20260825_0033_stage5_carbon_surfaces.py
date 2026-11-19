"""Add Stage 5 carbon read indexes.

Revision ID: 20260825_0033
Revises: 20260822_0032
Create Date: 2026-08-25
"""

from collections.abc import Sequence
import os

from alembic import op


revision: str = "20260825_0033"
down_revision: str | None = "20260822_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_rule_config_table()
    op.execute("DROP INDEX IF EXISTS carbon_assessments_current_cell_uq")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS carbon_assessments_current_pipeline_cell_uq
        ON carbon_assessments (
            grid_cell_id,
            (COALESCE(metadata->>'pipeline', 'legacy'))
        )
        WHERE is_current
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_stage5_current_cell_idx
        ON carbon_assessments (grid_cell_id, created_at DESC, id DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage5_carbon_assessment'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_stage5_status_anomaly_idx
        ON carbon_assessments (
            status,
            (metadata->>'anomaly_type'),
            created_at DESC,
            id DESC
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage5_carbon_assessment'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_stage5_created_cell_idx
        ON carbon_assessment_events (created_at DESC, id DESC, grid_cell_id)
        WHERE metadata->>'pipeline' = 'stage5_carbon_assessment_audit'
        """
    )
    _grant_runtime_access()


def downgrade() -> None:
    _revoke_runtime_access()
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_stage5_created_cell_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_stage5_status_anomaly_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_stage5_current_cell_idx")
    op.execute("DROP INDEX IF EXISTS stage5_rule_configs_active_scope_uq")
    op.execute("DROP INDEX IF EXISTS stage5_rule_configs_scope_created_idx")
    op.execute("DROP TABLE IF EXISTS stage5_rule_configs")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_current_pipeline_cell_uq")
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY grid_cell_id
                    ORDER BY created_at DESC, id DESC
                ) AS rank
            FROM carbon_assessments
            WHERE is_current
        )
        UPDATE carbon_assessments assessment
        SET is_current = false
        FROM ranked
        WHERE assessment.id = ranked.id
          AND ranked.rank > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS carbon_assessments_current_cell_uq
        ON carbon_assessments (grid_cell_id)
        WHERE is_current
        """
    )

def _create_rule_config_table() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage5_rule_configs (
            id BIGSERIAL PRIMARY KEY,
            region_code TEXT NOT NULL,
            grid_version TEXT NOT NULL,
            config_patch JSONB NOT NULL DEFAULT '{}'::jsonb,
            effective_config_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by TEXT NOT NULL DEFAULT 'api',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            retired_at TIMESTAMPTZ,
            CONSTRAINT stage5_rule_configs_status_valid CHECK (status IN ('active', 'retired')),
            CONSTRAINT stage5_rule_configs_region_not_blank CHECK (length(trim(region_code)) > 0),
            CONSTRAINT stage5_rule_configs_grid_not_blank CHECK (length(trim(grid_version)) > 0),
            CONSTRAINT stage5_rule_configs_hash_not_blank CHECK (length(trim(effective_config_hash)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stage5_rule_configs_active_scope_uq
        ON stage5_rule_configs (region_code, grid_version)
        WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage5_rule_configs_scope_created_idx
        ON stage5_rule_configs (region_code, grid_version, created_at DESC, id DESC)
        """
    )


def _grant_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE stage5_rule_configs TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE stage5_rule_configs_id_seq TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE carbon_assessments TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT SELECT, INSERT ON TABLE carbon_assessment_events TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE carbon_assessments_id_seq TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE carbon_assessment_events_id_seq TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE stage5_rule_configs TO {_quote_identifier(read_role)}';
                EXECUTE 'GRANT SELECT ON TABLE carbon_assessments TO {_quote_identifier(read_role)}';
                EXECUTE 'GRANT SELECT ON TABLE carbon_assessment_events TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _revoke_runtime_access() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'REVOKE SELECT, INSERT, UPDATE ON TABLE stage5_rule_configs FROM {_quote_identifier(app_role)}';
                EXECUTE 'REVOKE USAGE, SELECT ON SEQUENCE stage5_rule_configs_id_seq FROM {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'REVOKE SELECT ON TABLE stage5_rule_configs FROM {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
