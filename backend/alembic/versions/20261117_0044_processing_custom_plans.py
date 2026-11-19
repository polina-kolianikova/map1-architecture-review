"""Keep parameter-sensitive processing queries on custom execution plans."""

import os

from alembic import op


revision = "20261117_0044"
down_revision = "20261117_0043"
branch_labels = None
depends_on = None


def upgrade():
    app_role = os.getenv("POSTGRES_APP_USER", "map1_app")
    literal = "'" + app_role.replace("'", "''") + "'"
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {literal}) THEN
                EXECUTE format(
                    'ALTER ROLE %I IN DATABASE %I SET plan_cache_mode = force_custom_plan',
                    {literal}, current_database()
                );
            END IF;
        END $$;
        """
    )


def downgrade():
    # This processing setting may have been applied before the migration.
    pass
