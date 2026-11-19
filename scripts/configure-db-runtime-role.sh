#!/usr/bin/env sh
set -eu

COMPOSE_FILE_SET="${COMPOSE_FILE+x}"
if [ -z "${ENV_FILE:-}" ]; then
  if [ -f ".env" ]; then
    ENV_FILE=".env"
  elif [ -f "../map1-turkey-private/env/map1-turkey.env" ]; then
    ENV_FILE="../map1-turkey-private/env/map1-turkey.env"
    if [ -z "$COMPOSE_FILE_SET" ]; then
      COMPOSE_FILE="infra/docker-compose.yml"
    fi
  else
    ENV_FILE=".env"
  fi
fi
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.prod.yml}"
DB_SERVICE="${DB_SERVICE:-db}"

env_value() {
  key="$1"
  value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1)"
  if [ -z "$value" ]; then
    echo "missing $key in $ENV_FILE" >&2
    exit 1
  fi
  printf '%s' "$value"
}

validate_identifier() {
  name="$1"
  value="$2"
  case "$value" in
    [A-Za-z_]*)
      if printf '%s' "$value" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$'; then
        return 0
      fi
      ;;
  esac
  echo "$name must be a plain PostgreSQL identifier" >&2
  exit 1
}

db_name="$(env_value POSTGRES_DB)"
owner_user="$(env_value POSTGRES_USER)"
app_user="$(env_value POSTGRES_APP_USER)"
app_password="$(env_value POSTGRES_APP_PASSWORD)"
read_user="$(env_value POSTGRES_READ_USER)"
read_password="$(env_value POSTGRES_READ_PASSWORD)"

validate_identifier POSTGRES_DB "$db_name"
validate_identifier POSTGRES_USER "$owner_user"
validate_identifier POSTGRES_APP_USER "$app_user"
validate_identifier POSTGRES_READ_USER "$read_user"

if [ "$app_user" = "$owner_user" ]; then
  echo "POSTGRES_APP_USER must be different from POSTGRES_USER" >&2
  exit 1
fi

if [ "$read_user" = "$owner_user" ] || [ "$read_user" = "$app_user" ]; then
  echo "POSTGRES_READ_USER must be different from POSTGRES_USER and POSTGRES_APP_USER" >&2
  exit 1
fi

sql_literal() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"
}

app_password_literal="$(sql_literal "$app_password")"
read_password_literal="$(sql_literal "$read_password")"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -v ON_ERROR_STOP=1 \
    -U "$owner_user" \
    -d "$db_name" \
    -v app_user="$app_user" \
    -v read_user="$read_user" \
    -v db_name="$db_name" <<SQL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', $app_password_literal)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'read_user', $read_password_literal)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'read_user')
\gexec

SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_user', $app_password_literal)
\gexec

SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'read_user', $read_password_literal)
\gexec

SELECT format(
    'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'app_user'
)
\gexec

SELECT format(
    'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'read_user'
)
\gexec

SELECT format('GRANT CONNECT, TEMP ON DATABASE %I TO %I', :'db_name', :'app_user')
\gexec

SELECT format(
    'ALTER ROLE %I IN DATABASE %I SET plan_cache_mode = force_custom_plan',
    :'app_user', :'db_name'
)
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'read_user')
\gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user')
\gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'read_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', :'app_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', :'read_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', :'app_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', :'read_user')
\gexec

WITH table_name(name) AS (
    VALUES
        ('regions'),
        ('grid_cells'),
        ('ogm_parcels'),
        ('sentinel_observations'),
        ('verification_results'),
        ('source_checks'),
        ('carbon_assessments'),
        ('carbon_assessment_events'),
        ('alembic_version'),
        ('stage3_grid_generation_runs'),
        ('stage3_grid_generation_tiles'),
        ('stage3_source_mask_cache'),
        ('stage3_region_mask_components'),
        ('stage3_layer_a_ingest_runs'),
        ('stage3_layer_a_ingest_tiles'),
        ('stage4_layer_b_runs'),
        ('stage4_layer_b_batches'),
        ('stage4_layer_b_events'),
        ('stage4_layer_b_metrics'),
        ('stage4_satellite_artifacts'),
        ('stage4_process_heartbeats'),
        ('stage4_alerts'),
        ('stage5_carbon_summaries')
)
SELECT format('GRANT SELECT, INSERT ON TABLE public.%I TO %I', name, :'app_user')
FROM table_name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

SELECT format('GRANT UPDATE (name, country_code, geom, metadata) ON TABLE public.regions TO %I', :'app_user')
WHERE to_regclass('public.regions') IS NOT NULL
\gexec

WITH table_name(name) AS (
    VALUES
        ('ogm_parcels'),
        ('sentinel_observations'),
        ('sentinel_observations_partitioned'),
        ('verification_results'),
        ('carbon_assessments')
)
SELECT format('GRANT UPDATE (is_current) ON TABLE public.%I TO %I', name, :'app_user')
FROM table_name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

WITH table_name(name) AS (
    VALUES
        ('grid_cells'),
        ('grid_cells_partitioned')
)
SELECT format('GRANT UPDATE (area_coefficient) ON TABLE public.%I TO %I', name, :'app_user')
FROM table_name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

SELECT format('GRANT SELECT, INSERT ON TABLE public.sentinel_observations_partitioned TO %I', :'app_user')
WHERE to_regclass('public.sentinel_observations_partitioned') IS NOT NULL
\gexec

-- Layer C refreshes only the selected batch in its partitioned mirror.
SELECT format('GRANT SELECT, INSERT, DELETE ON TABLE public.verification_results_partitioned TO %I', :'app_user')
WHERE to_regclass('public.verification_results_partitioned') IS NOT NULL
\gexec

WITH table_name(name) AS (
    VALUES
        ('regions'),
        ('grid_cells'),
        ('grid_cells_partitioned'),
        ('ogm_parcels'),
        ('sentinel_observations'),
        ('sentinel_observations_partitioned'),
        ('verification_results'),
        ('verification_results_partitioned'),
        ('source_checks'),
        ('carbon_assessments'),
        ('carbon_assessment_events'),
        ('stage3_grid_generation_runs'),
        ('stage3_grid_generation_tiles'),
        ('stage3_source_mask_cache'),
        ('stage3_region_mask_components'),
        ('stage3_layer_a_ingest_runs'),
        ('stage3_layer_a_ingest_tiles'),
        ('stage4_layer_b_runs'),
        ('stage4_layer_b_batches'),
        ('stage4_layer_b_events'),
        ('stage4_layer_b_metrics'),
        ('stage4_satellite_artifacts'),
        ('stage4_process_heartbeats'),
        ('stage4_alerts'),
        ('stage5_rule_configs'),
        ('stage5_carbon_summaries')
)
SELECT format('GRANT SELECT ON TABLE public.%I TO %I', name, :'read_user')
FROM table_name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

WITH root_tables AS (
    SELECT cls.oid
    FROM pg_class cls
    JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
    WHERE namespace.nspname = 'public'
      AND cls.relname IN (
        'grid_cells_partitioned',
        'sentinel_observations_partitioned',
        'verification_results_partitioned'
      )
),
allowed_tables AS (
    SELECT oid FROM root_tables
    UNION
    SELECT inherit.inhrelid
    FROM pg_inherits inherit
    JOIN root_tables root ON root.oid = inherit.inhparent
)
SELECT format('GRANT SELECT ON TABLE %I.%I TO %I', namespace.nspname, cls.relname, :'read_user')
FROM pg_class cls
JOIN pg_namespace namespace ON namespace.oid = cls.relnamespace
JOIN allowed_tables allowed ON allowed.oid = cls.oid
WHERE cls.relkind IN ('r', 'p')
\gexec

WITH table_name(name) AS (
    VALUES
        ('stage3_grid_generation_runs'),
        ('stage3_grid_generation_tiles'),
        ('stage3_source_mask_cache'),
        ('stage3_region_mask_components'),
        ('stage3_layer_a_ingest_runs'),
        ('stage3_layer_a_ingest_tiles'),
        ('stage4_layer_b_runs'),
        ('stage4_layer_b_batches'),
        ('stage4_layer_b_events'),
        ('stage4_layer_b_metrics'),
        ('stage4_satellite_artifacts'),
        ('stage4_process_heartbeats'),
        ('stage4_alerts'),
        ('stage5_carbon_summaries')
)
SELECT format('GRANT UPDATE ON TABLE public.%I TO %I', name, :'app_user')
FROM table_name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

SELECT format('GRANT SELECT ON TABLE public.stage5_rule_configs TO %I', :'app_user')
WHERE to_regclass('public.stage5_rule_configs') IS NOT NULL
\gexec

SELECT format('REVOKE INSERT, UPDATE ON TABLE public.stage5_rule_configs FROM %I', :'app_user')
WHERE to_regclass('public.stage5_rule_configs') IS NOT NULL
\gexec

SELECT format(
    'GRANT INSERT (
        region_code,
        grid_version,
        config_patch,
        effective_config_hash,
        created_by,
        metadata
    ) ON TABLE public.stage5_rule_configs TO %I',
    :'app_user'
)
WHERE to_regclass('public.stage5_rule_configs') IS NOT NULL
\gexec

SELECT format(
    'GRANT UPDATE (
        status,
        retired_at
    ) ON TABLE public.stage5_rule_configs TO %I',
    :'app_user'
)
WHERE to_regclass('public.stage5_rule_configs') IS NOT NULL
\gexec

SELECT format('GRANT DELETE ON TABLE public.stage3_grid_generation_tiles TO %I', :'app_user')
WHERE to_regclass('public.stage3_grid_generation_tiles') IS NOT NULL
\gexec

WITH table_name(name) AS (
    VALUES
        ('stage3_source_mask_cache'),
        ('stage3_region_mask_components')
)
SELECT format('GRANT DELETE ON TABLE public.%I TO %I', name, :'app_user')
FROM table_name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_user')
\gexec

SELECT format('REVOKE UPDATE, DELETE, TRUNCATE ON public.verification_audit_events FROM %I', :'app_user')
WHERE to_regclass('public.verification_audit_events') IS NOT NULL
\gexec

SELECT format('GRANT SELECT, INSERT ON public.verification_audit_events TO %I', :'app_user')
WHERE to_regclass('public.verification_audit_events') IS NOT NULL
\gexec

SELECT format('GRANT SELECT ON public.verification_audit_events TO %I', :'read_user')
WHERE to_regclass('public.verification_audit_events') IS NOT NULL
\gexec

SELECT format('REVOKE UPDATE, DELETE, TRUNCATE ON public.carbon_assessment_events FROM %I', :'app_user')
WHERE to_regclass('public.carbon_assessment_events') IS NOT NULL
\gexec

SELECT format('GRANT SELECT, INSERT ON public.carbon_assessment_events TO %I', :'app_user')
WHERE to_regclass('public.carbon_assessment_events') IS NOT NULL
\gexec

SELECT format('GRANT SELECT ON public.carbon_assessment_events TO %I', :'read_user')
WHERE to_regclass('public.carbon_assessment_events') IS NOT NULL
\gexec
SQL

echo "runtime database roles configured"
