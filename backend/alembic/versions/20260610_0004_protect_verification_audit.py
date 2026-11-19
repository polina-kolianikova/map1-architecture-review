"""Protect verification audit events from mutation.

Revision ID: 20260610_0004
Revises: 20260608_0003
Create Date: 2026-06-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260610_0004"
down_revision: str | None = "20260608_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE verification_results "
        "ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY grid_cell_id
                    ORDER BY created_at DESC, id DESC
                ) AS row_number
            FROM verification_results
            WHERE is_current
              AND metadata->>'pipeline' = 'stage1_verification'
        )
        UPDATE verification_results result
        SET is_current = false
        FROM ranked
        WHERE ranked.id = result.id
          AND ranked.row_number > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS verification_results_current_stage1_cell_uq
        ON verification_results (grid_cell_id)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS verification_results_current_stage1_status_idx
        ON verification_results (anomaly_type, created_at DESC)
        WHERE is_current
          AND metadata->>'pipeline' = 'stage1_verification'
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_verification_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'verification_audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER verification_audit_events_no_update_delete
        BEFORE UPDATE OR DELETE ON verification_audit_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_verification_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS verification_audit_events_no_update_delete "
        "ON verification_audit_events"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_verification_audit_mutation()")
    op.execute("DROP INDEX IF EXISTS verification_results_current_stage1_status_idx")
    op.execute("DROP INDEX IF EXISTS verification_results_current_stage1_cell_uq")
    op.execute("ALTER TABLE verification_results DROP COLUMN IF EXISTS is_current")
