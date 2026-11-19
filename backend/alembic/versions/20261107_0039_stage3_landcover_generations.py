"""Track immutable land-cover generations and atomic window checkpoints.

The concurrent cache index is deliberately first: Alembic commits before its
autocommit block. A failed index build therefore cannot commit half the new
schema. A later transactional failure leaves only a reusable index behind.
"""
from alembic import op
from sqlalchemy import text

revision = '20261107_0039'
down_revision = '20261105_0038'
branch_labels = None
depends_on = None


_INDEX = 'public.stage3_landcover_cache_window_idx'
_LOCK = 'migration:20261107_0039:cache_window_index'
_LIMITS = {
    'lock_timeout': '5s',
    'statement_timeout': '15min',
    'maintenance_work_mem': '128MB',
    'max_parallel_maintenance_workers': '1',
}
_SETTINGS_SQL = text('SELECT ' + ','.join(
    f"current_setting('{name}') AS {name}" for name in _LIMITS))
_CONFIGURE_SQL = text('SELECT ' + ','.join(
    f"set_config('{name}', :{name}, false)" for name in _LIMITS))
_LOCK_SQL = text('SELECT pg_try_advisory_lock(hashtextextended(:key,0))')
_UNLOCK_SQL = text('SELECT pg_advisory_unlock(hashtextextended(:key,0))')
_INDEX_STATE_SQL = text(r"""
    SELECT CASE WHEN i.indexrelid IS NULL THEN false ELSE
      c.relkind='i' AND am.amname='btree'
      AND i.indrelid='public.stage3_source_mask_cache'::regclass
      AND i.indnkeyatts=2 AND i.indnatts=2
      AND NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion
      AND pg_get_indexdef(c.oid,1,false)='source_region_code'
      AND translate(pg_get_indexdef(c.oid,2,false),E' ()\t\n\r','')
          = $expr$metadata->>'window_id'::text$expr$
      AND translate(pg_get_expr(i.indpred,i.indrelid,false),E' ()\t\n\r','')
          = $expr$metadata->>'pipeline'::text='stage3_landcover_window'::text$expr$
      END AS expected,
      i.indisvalid AND i.indisready AND i.indislive AS usable
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_index i ON i.indexrelid=c.oid
    LEFT JOIN pg_am am ON am.oid=c.relam
    WHERE n.nspname='public' AND c.relname='stage3_landcover_cache_window_idx'
""")


def _index_state(connection):
    state = connection.execute(_INDEX_STATE_SQL).mappings().one_or_none()
    if state is not None and not state['expected']:
        raise RuntimeError('Cache window index has an unexpected definition; left unchanged')
    return state


def _cache_index(*, drop=False):
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError('Migration 0039 requires online index catalog validation')
    with context.autocommit_block():
        connection = op.get_bind()
        previous = dict(connection.execute(_SETTINGS_SQL).mappings().one())
        locked = False
        try:
            connection.execute(_CONFIGURE_SQL, _LIMITS)
            locked = connection.execute(_LOCK_SQL, {'key': _LOCK}).scalar_one()
            if not locked:
                raise RuntimeError('Another migration owns the cache window index; retry later')
            state = _index_state(connection)
            # Concurrent builds can leave an invalid index on timeout/cancel.
            # Only this exact index definition may be removed and rebuilt.
            if state is not None and (drop or not state['usable']):
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}')
                state = None
            if not drop:
                if state is None:
                    op.execute('''CREATE INDEX CONCURRENTLY IF NOT EXISTS stage3_landcover_cache_window_idx
                      ON public.stage3_source_mask_cache(source_region_code,(metadata->>'window_id'))
                      WHERE metadata->>'pipeline'='stage3_landcover_window' ''')
                state = _index_state(connection)
                if state is None or not state['usable']:
                    raise RuntimeError('Cache window index is not valid and ready; retry the migration')
        finally:
            try:
                if locked:
                    connection.execute(_UNLOCK_SQL, {'key': _LOCK})
            finally:
                connection.execute(_CONFIGURE_SQL, previous)


def _schema_limits():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='15min'")


def upgrade():
    _cache_index()
    _schema_limits()
    op.execute('''CREATE TABLE stage3_landcover_generations (
      code text PRIMARY KEY REFERENCES regions(code),
      plan_sha256 text NOT NULL CHECK(length(plan_sha256)=64),
      source_identity jsonb NOT NULL,
      status text NOT NULL DEFAULT 'loading' CHECK(status IN ('loading','sealed')),
      created_at timestamptz NOT NULL DEFAULT now(), sealed_at timestamptz
    )''')
    op.execute('''CREATE TABLE stage3_landcover_windows (
      generation text NOT NULL REFERENCES stage3_landcover_generations(code),
      window_id text NOT NULL, bbox geometry(Polygon,6933) NOT NULL,
      bundle_sha256 text, receipt jsonb,
      PRIMARY KEY(generation,window_id),
      CHECK ((bundle_sha256 IS NULL) = (receipt IS NULL))
    )''')
    op.execute('CREATE INDEX stage3_landcover_windows_gix ON stage3_landcover_windows USING gist(bbox)')
    op.execute('''DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='map1_app') THEN
        GRANT SELECT,INSERT,UPDATE ON stage3_landcover_generations,stage3_landcover_windows TO map1_app;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='map1_read') THEN
        GRANT SELECT ON stage3_landcover_generations,stage3_landcover_windows TO map1_read;
      END IF;
    END $$''')


def downgrade():
    _cache_index(drop=True)
    _schema_limits()
    op.execute('DROP TABLE stage3_landcover_windows')
    op.execute('DROP TABLE stage3_landcover_generations')
