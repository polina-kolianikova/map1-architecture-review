"""Add Stage 5 summary cache.

Revision ID: 20260826_0034
Revises: 20260825_0033
Create Date: 2026-08-26
"""

from collections.abc import Sequence
import os

from alembic import op


revision: str = "20260826_0034"
down_revision: str | None = "20260825_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_summary_table()
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_stage5_current_created_idx
        ON carbon_assessments (created_at DESC, id DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage5_carbon_assessment'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessments_stage5_anomaly_created_idx
        ON carbon_assessments (
            (metadata->>'anomaly_type'),
            created_at DESC,
            id DESC
        )
        WHERE is_current
          AND metadata->>'pipeline' = 'stage5_carbon_assessment'
        """
    )
    _grant_runtime_access()


def downgrade() -> None:
    _revoke_runtime_access()
    op.execute("DROP INDEX IF EXISTS carbon_assessments_stage5_anomaly_created_idx")
    op.execute("DROP INDEX IF EXISTS carbon_assessments_stage5_current_created_idx")
    op.execute("DROP INDEX IF EXISTS stage5_carbon_summaries_refreshed_idx")
    op.execute("DROP INDEX IF EXISTS stage5_carbon_summaries_scope_uq")
    op.execute("DROP TABLE IF EXISTS stage5_carbon_summaries")


def _create_summary_table() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stage5_carbon_summaries (
            id BIGSERIAL PRIMARY KEY,
            region_code TEXT NOT NULL,
            grid_version TEXT NOT NULL,
            pipeline TEXT NOT NULL DEFAULT 'stage5_carbon_assessment',
            source_layer_b_run_id BIGINT,
            total_cells BIGINT NOT NULL DEFAULT 0,
            carbon_assessments BIGINT NOT NULL DEFAULT 0,
            calculated_cells BIGINT NOT NULL DEFAULT 0,
            skipped_missing_evidence_cells BIGINT NOT NULL DEFAULT 0,
            baseline_cells BIGINT NOT NULL DEFAULT 0,
            historical_trend_cells BIGINT,
            current_observation_only_cells BIGINT,
            ok_cells BIGINT NOT NULL DEFAULT 0,
            watch_cells BIGINT NOT NULL DEFAULT 0,
            anomaly_cells BIGINT NOT NULL DEFAULT 0,
            insufficient_data_cells BIGINT NOT NULL DEFAULT 0,
            unverified_cells BIGINT NOT NULL DEFAULT 0,
            total_potential_tco2e NUMERIC,
            average_confidence_score NUMERIC,
            latest_calculated_at TIMESTAMPTZ,
            refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT stage5_carbon_summaries_region_not_blank CHECK (length(trim(region_code)) > 0),
            CONSTRAINT stage5_carbon_summaries_grid_not_blank CHECK (length(trim(grid_version)) > 0),
            CONSTRAINT stage5_carbon_summaries_pipeline_not_blank CHECK (length(trim(pipeline)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stage5_carbon_summaries_scope_uq
        ON stage5_carbon_summaries (region_code, grid_version, pipeline)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS stage5_carbon_summaries_refreshed_idx
        ON stage5_carbon_summaries (refreshed_at DESC, id DESC)
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
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE stage5_carbon_summaries TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE stage5_carbon_summaries_id_seq TO {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE stage5_carbon_summaries TO {_quote_identifier(read_role)}';
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
                EXECUTE 'REVOKE SELECT, INSERT, UPDATE ON TABLE stage5_carbon_summaries FROM {_quote_identifier(app_role)}';
                EXECUTE 'REVOKE USAGE, SELECT ON SEQUENCE stage5_carbon_summaries_id_seq FROM {_quote_identifier(app_role)}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'REVOKE SELECT ON TABLE stage5_carbon_summaries FROM {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
