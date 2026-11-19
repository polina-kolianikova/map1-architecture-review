-- Run only in the disposable restore database. Zero application rows are valid.
-- pg_restore has already checked archive data and restored constraints/indexes.
BEGIN;
SET LOCAL search_path = pg_catalog, public;
SELECT 'validation_mode=archive_schema_and_audit; counts=restored_only';

DO $$
DECLARE
    table_name text;
    alternatives text[];
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'alembic_version', 'regions', 'ogm_parcels', 'verification_audit_events'
    ] LOOP
        IF to_regclass('public.' || table_name) IS NULL THEN
            RAISE EXCEPTION 'restore validation: missing table %', table_name;
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM public.alembic_version WHERE length(trim(version_num)) > 0) THEN
        RAISE EXCEPTION 'restore validation: missing migration version';
    END IF;
    FOREACH alternatives SLICE 1 IN ARRAY ARRAY[
        ['grid_cells', 'grid_cells_partitioned'],
        ['sentinel_observations', 'sentinel_observations_partitioned'],
        ['verification_results', 'verification_results_partitioned']
    ] LOOP
        IF to_regclass('public.' || alternatives[1]) IS NULL
           AND to_regclass('public.' || alternatives[2]) IS NULL THEN
            RAISE EXCEPTION 'restore validation: missing table family %', alternatives;
        END IF;
    END LOOP;
    IF to_regclass('public.carbon_assessments') IS NOT NULL
       AND to_regclass('public.carbon_assessment_events') IS NULL THEN
        RAISE EXCEPTION 'restore validation: missing carbon audit table';
    END IF;
END $$;

-- Count roots, including their partitions. Do not double-count child tables or
-- assume that legacy and partition shadows contain the same data.
SELECT format('SELECT %L || count(*) FROM public.%I;', relname || '=', relname)
FROM pg_class
WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p')
  AND relname IN (
      'regions', 'grid_cells', 'grid_cells_partitioned', 'ogm_parcels',
      'sentinel_observations', 'sentinel_observations_partitioned',
      'verification_results', 'verification_results_partitioned',
      'verification_audit_events', 'carbon_assessments', 'carbon_assessment_events',
      'stage4_layer_b_runs', 'stage4_layer_b_batches', 'stage4_layer_b_events',
      'stage4_layer_b_metrics', 'stage4_satellite_artifacts',
      'stage4_process_heartbeats', 'stage4_alerts',
      'stage5_rule_configs', 'stage5_carbon_summaries'
  )
ORDER BY relname
\gexec

DO $$
DECLARE
    audit_table text;
    probe_id bigint;
    operation text;
    rejected boolean;
BEGIN
    FOREACH audit_table IN ARRAY ARRAY['verification_audit_events', 'carbon_assessment_events'] LOOP
        IF to_regclass('public.' || audit_table) IS NULL THEN
            CONTINUE; -- Carbon is optional for older schema versions.
        END IF;
        -- Seed our own row so row triggers are exercised even in an empty DB.
        -- The outer transaction rolls back every probe, including successful ones.
        IF audit_table = 'verification_audit_events' THEN
            INSERT INTO public.verification_audit_events
                (event_type, new_status, new_score, reason, source_data_version)
            VALUES ('created', 'backup_restore_probe', 0, 'restore check', 'restore-check')
            RETURNING id INTO probe_id;
        ELSE
            INSERT INTO public.carbon_assessment_events (event_type, new_status)
            VALUES ('created', 'backup_restore_probe') RETURNING id INTO probe_id;
        END IF;
        FOREACH operation IN ARRAY ARRAY['UPDATE', 'DELETE', 'TRUNCATE'] LOOP
            rejected := false;
            BEGIN
                IF operation = 'UPDATE' THEN
                    EXECUTE format('UPDATE public.%I SET event_type = event_type WHERE id = $1', audit_table)
                    USING probe_id;
                ELSIF operation = 'DELETE' THEN
                    EXECUTE format('DELETE FROM public.%I WHERE id = $1', audit_table) USING probe_id;
                ELSE
                    EXECUTE format('TRUNCATE public.%I', audit_table);
                END IF;
            EXCEPTION WHEN raise_exception THEN
                -- Permissions, FK errors, or unrelated SQL failures are not proof
                -- that the restored append-only guard works.
                IF SQLERRM <> audit_table || ' is append-only' THEN
                    RAISE;
                END IF;
                rejected := true;
            END;
            IF NOT rejected THEN
                RAISE EXCEPTION 'restore validation: % allowed %', audit_table, operation;
            END IF;
        END LOOP;
    END LOOP;
END $$;
ROLLBACK;
