"""Allow the application writer to maintain the partitioned verification mirror."""

import os

from alembic import op


revision = "20261117_0041"
down_revision = "20261115_0040"
branch_labels = None
depends_on = None


def upgrade():
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    role_literal = "'" + app_role.replace("'", "''") + "'"
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.verification_results_partitioned') IS NOT NULL
               AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN
                EXECUTE format(
                    'GRANT SELECT, INSERT, DELETE ON TABLE public.verification_results_partitioned TO %I',
                    {role_literal}
                );
            END IF;
        END $$;
        """
    )


def downgrade():
    # Existing installations may have granted these rights before this migration.
    # Retain them so a schema rollback cannot break the verification producer.
    pass
