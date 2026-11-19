"""Index source-cache lookups by source parcel.

Revision ID: 20261105_0038
Revises: 20260830_0037
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261105_0038"
down_revision: str | None = "20260830_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS stage3_source_mask_cache_parcel_idx
                ON public.stage3_source_mask_cache (source_parcel_id);
            """
        )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                JOIN pg_am am ON am.oid = c.relam
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                WHERE i.indexrelid = 'public.stage3_source_mask_cache_parcel_idx'::regclass
                  AND i.indrelid = 'public.stage3_source_mask_cache'::regclass
                  AND i.indisvalid AND i.indisready
                  AND i.indnkeyatts = 1 AND i.indnatts = 1
                  AND i.indpred IS NULL AND i.indexprs IS NULL
                  AND a.attname = 'source_parcel_id' AND am.amname = 'btree'
            ) THEN
                RAISE EXCEPTION 'Source cache lookup index is not ready or has an unexpected definition';
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.stage3_source_mask_cache_parcel_idx")
