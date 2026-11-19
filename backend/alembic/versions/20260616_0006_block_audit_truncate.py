"""Block audit table truncation.

Revision ID: 20260616_0006
Revises: 20260616_0005
Create Date: 2026-06-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260616_0006"
down_revision: str | None = "20260616_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TRIGGER verification_audit_events_no_truncate
        BEFORE TRUNCATE ON verification_audit_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION prevent_verification_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS verification_audit_events_no_truncate "
        "ON verification_audit_events"
    )
