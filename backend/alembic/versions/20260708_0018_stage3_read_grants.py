"""Grant read access to Stage 3 tables.

Revision ID: 20260708_0018
Revises: 20260707_0017
Create Date: 2026-07-08
"""

import os

from alembic import op


revision = "20260708_0018"
down_revision = "20260707_0017"
branch_labels = None
depends_on = None


TABLES = (
    "carbon_assessments",
    "carbon_assessment_events",
    "stage3_grid_generation_runs",
    "stage3_grid_generation_tiles",
)


def upgrade() -> None:
    read_role = _read_role()
    table_list = ", ".join(_quote_identifier(table) for table in TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE {table_list} TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    read_role = _read_role()
    table_list = ", ".join(_quote_identifier(table) for table in TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'REVOKE SELECT ON TABLE {table_list} FROM {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def _read_role() -> str:
    return os.getenv("POSTGRES_READ_USER", "map1_read")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
