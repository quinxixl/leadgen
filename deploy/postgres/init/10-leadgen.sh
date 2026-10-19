#!/bin/sh
# First start of an empty data volume only (docker-entrypoint-initdb.d): what Supabase provided and the
# migrations rely on, plus a least-privilege application role. Every step is idempotent, so the file can be
# re-run by hand, e.g. after a password change in .env.db:
#   docker compose exec db sh /docker-entrypoint-initdb.d/10-leadgen.sh
# Passwords are read by psql from the environment (\getenv), never passed on a command line.
set -eu

: "${LEADGEN_DB_PASSWORD:?Задайте LEADGEN_DB_PASSWORD в .env.db}"
export LEADGEN_DB_USER="${LEADGEN_DB_USER:-leadgen}"
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_DB=${POSTGRES_DB:-leadgen}
PGDATA=${PGDATA:-/var/lib/postgresql/data}

# The superuser may log in only from inside the container (socket, loopback). Remote TCP logins as it are
# rejected before the image's generic `host all all all scram-sha-256` rule; the app role is unaffected.
reject="host all $POSTGRES_USER all reject"
if ! grep -qx "$reject" "$PGDATA/pg_hba.conf"; then
    sed -i "/^host all all all /i $reject" "$PGDATA/pg_hba.conf"
    grep -qx "$reject" "$PGDATA/pg_hba.conf" || { echo "pg_hba.conf: правило reject не добавлено" >&2; exit 1; }
fi

psql -v ON_ERROR_STOP=1 -X -q --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_user LEADGEN_DB_USER
\getenv app_password LEADGEN_DB_PASSWORD

-- Supabase API roles: the migrations revoke from them, so they must exist. No login, no privileges.
SELECT format('CREATE ROLE %I NOLOGIN', r) FROM unnest(ARRAY['anon','authenticated']) AS r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=r) \gexec

-- The application role owns the database and every object it creates or restores; it is not a superuser.
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:'app_user') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_user', :'app_password') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', current_database(), :'app_user') \gexec
-- The limits the code was written against on Supabase (leadgen.product.cursor_lag_seconds relies on them:
-- no app transaction outlives the 600 s delivery-cursor lag). web_migrate raises them per transaction itself,
-- pg_restore/pg_dump set their own; superuser maintenance (vacuumdb, backups) is not affected.
SELECT format('ALTER ROLE %I SET statement_timeout = %L', :'app_user', '120s') \gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', :'app_user', '60s') \gexec

-- Schema public stays with the superuser: the app may use it, not create objects in it.
ALTER SCHEMA public OWNER TO postgres;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user') \gexec

-- Supabase keeps extensions in schema `extensions`: 20261014140000_typed_lead_columns.sql requires pg_trgm there
-- and the trigram index names extensions.gin_trgm_ops. Installed by the superuser, used by the app role.
CREATE SCHEMA IF NOT EXISTS extensions AUTHORIZATION postgres;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA extensions;
SELECT format('GRANT USAGE ON SCHEMA extensions TO %I', :'app_user') \gexec

-- 20260930093818_harden_multiuser_schema.sql revokes EXECUTE on this Supabase helper (an event trigger function
-- that enables RLS on new tables). A no-op stand-in, never attached to an event trigger, keeps that file runnable
-- when a database is built from the migration files instead of from a dump.
CREATE OR REPLACE FUNCTION public.rls_auto_enable() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN END $$;
SELECT format('ALTER FUNCTION public.rls_auto_enable() OWNER TO %I', :'app_user') \gexec

-- Statistics start clean (init itself is not interesting). On a manual re-run pg_hba.conf changes take effect
-- now; during first init (temporary server without TCP) the real start right after picks them up.
SELECT extensions.pg_stat_statements_reset();
SELECT pg_reload_conf() WHERE current_setting('listen_addresses') <> '';
SQL
