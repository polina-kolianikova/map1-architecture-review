"""Index both current and retired verification rows used by mirror maintenance."""

from alembic import op


revision = "20261117_0043"
down_revision = "20261117_0042"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_results_partitioned_cell_id_idx
        ON public.verification_results_partitioned
            (region_id, grid_version, grid_cell_id, id)
        """
    )


def downgrade():
    # Retain an index that may have existed before this migration.
    pass
