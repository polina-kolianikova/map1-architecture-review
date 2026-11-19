"""Add verification metadata.

Revision ID: 20260528_0002
Revises: 20260527_0001
Create Date: 2026-05-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260528_0002"
down_revision: str | None = "20260527_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE verification_results
        ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute("CREATE INDEX verification_results_metadata_gin ON verification_results USING GIN (metadata)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_results_metadata_gin")
    op.execute("ALTER TABLE verification_results DROP COLUMN IF EXISTS metadata")
