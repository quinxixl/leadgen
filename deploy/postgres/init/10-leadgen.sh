#!/bin/sh
# First start of an empty data volume only (docker-entrypoint-initdb.d): what Supabase provided and the
# migrations rely on, plus a least-privilege application role. Every statement is idempotent, so the
# file can be re-run by hand: docker compose exec -T db sh /docker-entrypoint-initdb.d/10-leadgen.sh
set -eu

: "${LEADGEN_DB_PASSWORD:?Задайте LEADGEN_DB_PASSWORD в .env.db}"
LEADGEN_DB_USER=${LEADGEN_DB_USER:-leadgen}
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_DB=${POSTGRES_DB:-leadgen}

psql -v ON_ERROR_STOP=1 -X -q --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_user="$LEADGEN_DB_USER" -v app_password="$LEADGEN_DB_PASSWORD" <<'SQL'
-- Supabase API roles: the migrations revoke from them, so they must exist. No login, no privileges.
SELECT format('CREATE ROLE %I NOLOGIN', r) FROM unnest(ARRAY['anon','authenticated']) AS r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=r) \gexec

-- The application role owns the database and every object it creates or restores; it is not a superuser.
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:'app_user') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_user', :'app_password') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', current_database(), :'app_user') \gexec
-- web_migrate logs lock holders from pg_stat_activity; pg_stat_statements for slow-query analysis.
SELECT format('GRANT pg_read_all_stats TO %I', :'app_user') \gexec

-- Supabase keeps extensions in schema `extensions`: 20261014140000_typed_lead_columns.sql requires pg_trgm there
-- and the trigram index names extensions.gin_trgm_ops. Installed by the superuser, used by the app role.
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA extensions;
SELECT format('GRANT USAGE ON SCHEMA extensions TO %I', :'app_user') \gexec

-- 20260930093818_harden_multiuser_schema.sql revokes EXECUTE on this Supabase helper (an event trigger function
-- that enables RLS on new tables). A no-op stand-in, never attached to an event trigger, keeps that file runnable
-- when a database is built from the migration files instead of from a dump.
CREATE OR REPLACE FUNCTION public.rls_auto_enable() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN END $$;
SELECT format('ALTER FUNCTION public.rls_auto_enable() OWNER TO %I', :'app_user') \gexec

-- PG 15+ already denies CREATE on schema public to PUBLIC; keep it explicit.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
