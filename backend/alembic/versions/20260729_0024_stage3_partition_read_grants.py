"""Grant runtime read access to Stage 3 partition shadows.

Revision ID: 20260729_0024
Revises: 20260727_0023
Create Date: 2026-07-29
"""

from collections.abc import Sequence
import os

from alembic import op


revision: str = "20260729_0024"
down_revision: str | None = "20260727_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ROOT_TABLES = (
    "grid_cells_partitioned",
    "sentinel_observations_partitioned",
    "verification_results_partitioned",
)


def upgrade() -> None:
    read_role = _read_role()
    op.execute(
        f"""
        DO $$
        DECLARE
            partition_table record;
            read_role text := {_quote_literal(read_role)};
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = read_role) THEN
                FOR partition_table IN
                    WITH root_tables AS (
                        SELECT cls.oid
                        FROM pg_class cls
                        JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND cls.relname IN ({_quoted_table_literals(ROOT_TABLES)})
                    ),
                    allowed_tables AS (
                        SELECT oid FROM root_tables
                        UNION
                        SELECT inherit.inhrelid
                        FROM pg_inherits inherit
                        JOIN root_tables root ON root.oid = inherit.inhparent
                    )
                    SELECT namespace.nspname, cls.relname
                    FROM pg_class cls
                    JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
                    JOIN allowed_tables allowed ON allowed.oid = cls.oid
                    WHERE cls.relkind IN ('r', 'p')
                LOOP
                    EXECUTE format(
                        'GRANT SELECT ON TABLE %I.%I TO %I',
                        partition_table.nspname,
                        partition_table.relname,
                        read_role
                    );
                END LOOP;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    read_role = _read_role()
    op.execute(
        f"""
        DO $$
        DECLARE
            partition_table record;
            read_role text := {_quote_literal(read_role)};
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = read_role) THEN
                FOR partition_table IN
                    WITH root_tables AS (
                        SELECT cls.oid
                        FROM pg_class cls
                        JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND cls.relname IN ({_quoted_table_literals(ROOT_TABLES)})
                    ),
                    allowed_tables AS (
                        SELECT oid FROM root_tables
                        UNION
                        SELECT inherit.inhrelid
                        FROM pg_inherits inherit
                        JOIN root_tables root ON root.oid = inherit.inhparent
                    )
                    SELECT namespace.nspname, cls.relname
                    FROM pg_class cls
                    JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
                    JOIN allowed_tables allowed ON allowed.oid = cls.oid
                    WHERE cls.relkind IN ('r', 'p')
                LOOP
                    EXECUTE format(
                        'REVOKE SELECT ON TABLE %I.%I FROM %I',
                        partition_table.nspname,
                        partition_table.relname,
                        read_role
                    );
                END LOOP;
            END IF;
        END $$;
        """
    )


def _read_role() -> str:
    return os.getenv("POSTGRES_READ_USER", "map1_read")


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quoted_table_literals(values: tuple[str, ...]) -> str:
    return ", ".join(_quote_literal(value) for value in values)
