"""Cover dashboard observation counts without reading observation metadata."""
from __future__ import annotations

import hashlib

from alembic import op
from sqlalchemy import text

revision = "20261117_0045"
down_revision = "20261117_0044"
branch_labels = None
depends_on = None

SUPPORTED = """is_current AND (metadata->>'pipeline', metadata->>'statistics_source') IN (
    ('stage1_cdse_ndvi', 'sentinel_hub_statistical_api'),
    ('stage4_layer_b_ndvi', 'sentinel_hub_statistical_api'),
    ('stage4_layer_b_ndvi', 'public_raster_zonal')
)"""
INDEXES = [
    ("all", "(region_id, grid_version, grid_cell_id, acquisition_date DESC, id DESC) INCLUDE (region_code)", SUPPORTED),
    ("valid", "(region_id, grid_version, id) INCLUDE (region_code)", SUPPORTED + " AND ndvi IS NOT NULL AND COALESCE(metadata->>'status', CASE WHEN ndvi IS NOT NULL THEN 'ok' END) = 'ok'"),
]


def _child_name(table, kind):
    if table == "sentinel_observations_partitioned_default":
        return "sentinel_observations_default_dashboard_" + kind + "_idx"
    return "so_dashboard_" + kind + "_" + hashlib.sha256(table.encode()).hexdigest()[:12]


def upgrade():
    connection = op.get_bind()
    quote = connection.dialect.identifier_preparer.quote
    children = connection.execute(text("""
        SELECT child.relname FROM pg_inherits
        JOIN pg_class child ON child.oid=inhrelid
        WHERE inhparent='sentinel_observations_partitioned'::regclass
        ORDER BY child.relname
    """)).scalars().all()
    for kind, columns, predicate in INDEXES:
        parent = "sentinel_observations_part_dashboard_" + kind + "_idx"
        for child in children:
            name = _child_name(child, kind)
            with op.get_context().autocommit_block():
                op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {quote(name)} ON {quote(child)} {columns} WHERE {predicate}")
        op.execute(f"CREATE INDEX IF NOT EXISTS {quote(parent)} ON ONLY sentinel_observations_partitioned {columns} WHERE {predicate}")
        for child in children:
            name = _child_name(child, kind)
            attached = connection.execute(text("""
                SELECT EXISTS(SELECT 1 FROM pg_inherits
                    WHERE inhrelid=to_regclass(:child) AND inhparent=to_regclass(:parent))
            """), {"child": name, "parent": parent}).scalar_one()
            if not attached:
                op.execute(f"ALTER INDEX {quote(parent)} ATTACH PARTITION {quote(name)}")


def downgrade():
    for kind, _columns, _predicate in reversed(INDEXES):
        op.execute("DROP INDEX IF EXISTS sentinel_observations_part_dashboard_" + kind + "_idx")
