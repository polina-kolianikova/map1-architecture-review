"""Add Stage 4 run lookup indexes.

Revision ID: 20260822_0032
Revises: 20260817_0031
Create Date: 2026-08-22
"""

from alembic import op


revision = "20260822_0032"
down_revision = "20260817_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_index(
        "stage4_layer_b_metrics_run_status_idx",
        "stage4_layer_b_metrics",
        "(run_id, metric_name, source_status, grid_cell_id)",
        tablespace="stage4_metrics_ts",
    )
    _create_index(
        "stage4_satellite_artifacts_run_provider_type_idx",
        "stage4_satellite_artifacts",
        "(run_id, provider, artifact_type, id)",
    )
    _create_index(
        "stage4_alerts_run_status_idx",
        "stage4_alerts",
        "(run_id, status, id)",
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS stage4_alerts_run_status_idx")
    op.execute("DROP INDEX IF EXISTS stage4_satellite_artifacts_run_provider_type_idx")
    op.execute("DROP INDEX IF EXISTS stage4_layer_b_metrics_run_status_idx")


def _create_index(
    index_name: str,
    table_name: str,
    columns: str,
    *,
    tablespace: str | None = None,
) -> None:
    if tablespace:
        op.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE 'CREATE INDEX IF NOT EXISTS {index_name} '
                    || 'ON {table_name} {columns}'
                    || (CASE WHEN EXISTS (
                        SELECT 1 FROM pg_tablespace WHERE spcname = '{tablespace}'
                    ) THEN ' TABLESPACE {tablespace}' ELSE '' END);
            END $$;
            """
        )
        return
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {index_name}
        ON {table_name} {columns}
        """
    )
