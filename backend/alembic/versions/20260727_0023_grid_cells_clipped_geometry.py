"""Allow clipped grid cell geometry.

Revision ID: 20260727_0023
Revises: 20260726_0022
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260727_0023"
down_revision: str | None = "20260726_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE grid_cells
        ALTER COLUMN geom TYPE geometry(Geometry, 4326)
        USING ST_SetSRID(geom, 4326)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE grid_cells
        ALTER COLUMN geom TYPE geometry(Polygon, 4326)
        USING ST_GeometryN(
            ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)),
            1
        )::geometry(Polygon, 4326)
        """
    )
