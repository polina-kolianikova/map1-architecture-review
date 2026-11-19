"""Collect source-admission statistics for partitioned dashboard reads."""
from alembic import op

revision = "20261117_0046"
down_revision = "20261117_0045"
branch_labels = None
depends_on = None

EXPRESSION = """(metadata->>'pipeline'), (metadata->>'statistics_source'),
    (COALESCE(metadata->>'status', CASE WHEN ndvi IS NOT NULL THEN 'ok' END)),
    (ndvi IS NOT NULL), is_current"""


def upgrade():
    for suffix, table in [
        ("parent", "sentinel_observations_partitioned"),
        ("default", "sentinel_observations_partitioned_default"),
    ]:
        op.execute("CREATE STATISTICS IF NOT EXISTS so_dashboard_admission_" + suffix
                   + " (mcv, dependencies) ON " + EXPRESSION + " FROM " + table)
        op.execute("ANALYZE " + table)
    op.execute("ANALYZE verification_results_partitioned")


def downgrade():
    for suffix in ("default", "parent"):
        op.execute("DROP STATISTICS IF EXISTS so_dashboard_admission_" + suffix)
