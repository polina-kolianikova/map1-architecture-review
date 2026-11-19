"""Protect current Layer A rows from duplicate refreshes.

Revision ID: 20260616_0007
Revises: 20260616_0006
Create Date: 2026-06-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260616_0007"
down_revision: str | None = "20260616_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY
                        source_layer,
                        source_id,
                        attributes->>'region_code'
                    ORDER BY last_seen_at DESC NULLS LAST, fetched_at DESC, id DESC
                ) AS row_number
            FROM ogm_parcels
            WHERE is_current
              AND source_id IS NOT NULL
        )
        UPDATE ogm_parcels parcel
        SET is_current = false
        FROM ranked
        WHERE ranked.id = parcel.id
          AND ranked.row_number > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ogm_parcels_current_region_source_uq
        ON ogm_parcels (
            source_layer,
            source_id,
            (attributes->>'region_code')
        )
        WHERE is_current
          AND source_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ogm_parcels_current_region_source_uq")
