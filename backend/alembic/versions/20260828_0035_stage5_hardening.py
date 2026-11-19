"""Harden Stage 5 rules and audit reads.

Revision ID: 20260828_0035
Revises: 20260826_0034
Create Date: 2026-08-28
"""

from collections.abc import Sequence
import os

from alembic import op


revision: str = "20260828_0035"
down_revision: str | None = "20260826_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS carbon_assessment_events_stage5_assessment_cell_idx
        ON carbon_assessment_events (carbon_assessment_id, grid_cell_id)
        WHERE metadata->>'pipeline' = 'stage5_carbon_assessment_audit'
        """
    )
    _tighten_stage5_rule_grants()


def downgrade() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    op.execute("DROP INDEX IF EXISTS carbon_assessment_events_stage5_assessment_cell_idx")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE stage5_rule_configs TO {_quote_identifier(app_role)}';
            END IF;
        END $$;
        """
    )


def _tighten_stage5_rule_grants() -> None:
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'REVOKE INSERT, UPDATE ON TABLE stage5_rule_configs FROM {_quote_identifier(app_role)}';
                EXECUTE 'GRANT INSERT (
                    region_code,
                    grid_version,
                    config_patch,
                    effective_config_hash,
                    created_by,
                    metadata
                ) ON TABLE stage5_rule_configs TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT UPDATE (
                    status,
                    retired_at
                ) ON TABLE stage5_rule_configs TO {_quote_identifier(app_role)}';
            END IF;
        END $$;
        """
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
