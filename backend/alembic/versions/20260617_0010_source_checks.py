"""Add source check history.

Revision ID: 20260617_0010
Revises: 20260616_0009
Create Date: 2026-06-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260617_0010"
down_revision: str | None = "20260616_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE source_checks (
            id BIGSERIAL PRIMARY KEY,
            source_layer TEXT NOT NULL,
            source_type TEXT NOT NULL,
            region_code TEXT NOT NULL,
            checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            status TEXT NOT NULL,
            feature_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT source_checks_status_check CHECK (
                status IN ('ok', 'unavailable', 'empty')
            ),
            CONSTRAINT source_checks_feature_count_check CHECK (feature_count >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX source_checks_latest_idx
        ON source_checks (region_code, source_layer, checked_at DESC, id DESC)
        """
    )
    op.execute("CREATE INDEX source_checks_metadata_gin ON source_checks USING GIN (metadata)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_checks")
