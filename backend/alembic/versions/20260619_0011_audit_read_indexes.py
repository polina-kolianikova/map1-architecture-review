"""Add audit read indexes.

Revision ID: 20260619_0011
Revises: 20260617_0010
Create Date: 2026-06-19
"""

from alembic import op


revision = "20260619_0011"
down_revision = "20260617_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_audit_events_stage1_scope_idx
        ON verification_audit_events (
            (metadata->>'region_code'),
            ((metadata->'latest_result_metadata')->>'grid_version'),
            created_at DESC,
            id DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_audit_events_cell_uuid_idx
        ON verification_audit_events (
            ((metadata->'latest_result_metadata')->>'cell_uuid'),
            created_at DESC,
            id DESC
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS verification_audit_events_cell_uuid_idx")
    op.execute("DROP INDEX IF EXISTS verification_audit_events_stage1_scope_idx")
