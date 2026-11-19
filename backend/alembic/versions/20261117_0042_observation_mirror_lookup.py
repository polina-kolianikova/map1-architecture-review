"""Index observation identity lookups used by the public raster importer."""

from alembic import op


revision = "20261117_0042"
down_revision = "20261117_0041"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS sentinel_observations_partitioned_region_id_idx
        ON public.sentinel_observations_partitioned (region_id, id)
        """
    )


def downgrade():
    # An existing installation may already depend on this lookup index.
    pass
