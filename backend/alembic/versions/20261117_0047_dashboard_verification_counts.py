"""Cover current verification and evidence-log dashboard counts."""
from alembic import op
from sqlalchemy import text

revision = "20261117_0047"
down_revision = "20261117_0046"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    quote = connection.dialect.identifier_preparer.quote
    children = connection.execute(text("""
        SELECT child.relname FROM pg_inherits
        JOIN pg_class child ON child.oid=inhrelid
        WHERE inhparent='verification_results_partitioned'::regclass
        ORDER BY child.relname
    """)).scalars().all()
    columns = "(region_id, grid_version, grid_cell_id) INCLUDE (region_code, anomaly_type)"
    predicate = "is_current AND metadata->>'pipeline'='stage1_verification'"
    parent = "verification_results_dashboard_counts_idx"
    for child in children:
        with op.get_context().autocommit_block():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {quote(child + '_dashboard_counts_idx')} ON {quote(child)} {columns} WHERE {predicate}")
    op.execute(f"CREATE INDEX IF NOT EXISTS {parent} ON ONLY verification_results_partitioned {columns} WHERE {predicate}")
    for child in children:
        name = child + "_dashboard_counts_idx"
        attached = connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=to_regclass(:child) AND inhparent=to_regclass(:parent))"), {"child": name, "parent": parent}).scalar_one()
        if not attached:
            op.execute(f"ALTER INDEX {parent} ATTACH PARTITION {quote(name)}")
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS verification_audit_dashboard_counts_idx ON verification_audit_events (region_id, grid_version) INCLUDE (region_code) WHERE metadata->>'pipeline'='stage1_verification_audit'")


def downgrade():
    op.execute("DROP INDEX IF EXISTS verification_audit_dashboard_counts_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_dashboard_counts_idx")
