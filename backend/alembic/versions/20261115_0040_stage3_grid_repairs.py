"""Append-only before/after records for bounded Stage 3 grid corrections."""
from alembic import op

revision = '20261115_0040'
down_revision = '20261107_0039'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS public.stage3_grid_repairs (
      repair_id text PRIMARY KEY,
      region_id bigint NOT NULL REFERENCES public.regions(id),
      grid_run_id bigint NOT NULL REFERENCES public.stage3_grid_generation_runs(id),
      grid_version text NOT NULL,plan_sha256 text NOT NULL,previous_revision text NOT NULL,
      revision text NOT NULL UNIQUE,bounds_6933 jsonb NOT NULL,receipt jsonb NOT NULL,
      before_images jsonb NOT NULL,after_images jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK(length(plan_sha256)=64 AND length(revision)=64))""")
    op.execute('CREATE INDEX IF NOT EXISTS stage3_grid_repairs_scope_idx ON public.stage3_grid_repairs '
               '(region_id,grid_version,created_at)')
    op.execute("""CREATE OR REPLACE FUNCTION public.protect_stage3_grid_repair_history() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Stage 3 repair history is append-only';
      END $$""")
    op.execute('DROP TRIGGER IF EXISTS stage3_grid_repairs_immutable ON public.stage3_grid_repairs')
    op.execute("""CREATE TRIGGER stage3_grid_repairs_immutable BEFORE UPDATE OR DELETE
      ON public.stage3_grid_repairs FOR EACH ROW EXECUTE FUNCTION public.protect_stage3_grid_repair_history()""")


def downgrade():
    # Removing this ledger would discard the only transactional before-images.
    raise RuntimeError('retain or export Stage 3 repair history before a schema rollback')
