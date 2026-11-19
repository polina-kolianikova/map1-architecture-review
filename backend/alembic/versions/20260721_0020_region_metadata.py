"""Add region metadata.

Revision ID: 20260721_0020
Revises: 20260713_0019
Create Date: 2026-07-21
"""

from collections.abc import Sequence
import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260721_0020"
down_revision: str | None = "20260713_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "regions",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute("CREATE INDEX regions_metadata_gin ON regions USING GIN (metadata)")
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    read_role = os.getenv("POSTGRES_READ_USER", "map1_read")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(app_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE regions TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT INSERT (name, code, country_code, geom, metadata) ON TABLE regions TO {_quote_identifier(app_role)}';
                EXECUTE 'GRANT UPDATE (name, country_code, geom, metadata) ON TABLE regions TO {_quote_identifier(app_role)}';
                IF to_regclass('public.regions_id_seq') IS NOT NULL THEN
                    EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE regions_id_seq TO {_quote_identifier(app_role)}';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(read_role)}) THEN
                EXECUTE 'GRANT SELECT ON TABLE regions TO {_quote_identifier(read_role)}';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS regions_metadata_gin")
    op.drop_column("regions", "metadata")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
