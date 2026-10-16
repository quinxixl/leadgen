-- leads.first_seen / leads.sent_at: text -> timestamptz, leads.payload: text -> jsonb, plus leads.source.
--
-- Every query cast these columns on the fly (first_seen::timestamptz, payload::jsonb->>'source'), so nothing about
-- them could be indexed and every read parsed the JSON again. The code writes first_seen/sent_at only as
-- datetime.now(UTC).isoformat() ('2026-10-14T09:00:00.123456+00:00') and payload only as json.dumps(Lead.data()),
-- and the existing ::timestamptz / ::jsonb casts already parse exactly these values. The conversion runs in UTC so a
-- value without an offset (none are written, but old SQLite imports could have them) is read as UTC, as Supabase did.
-- An empty sent_at becomes NULL. Any other unparsable value makes the ALTER fail: web_migrate rolls the whole batch
-- back, nothing changes and the deploy stops before the new code starts.
--
-- source is a stored generated column (payload->>'source'): the leads page lists and filters by source on every load.
-- One ALTER TABLE statement, so the table and its indexes are rewritten once, under ACCESS EXCLUSIVE.
-- Measured locally: 100 000 rows / 95 MB in about 2 s; see the PR for the production estimate.
-- Idempotent: skipped when payload is already jsonb (tests re-run migration files).
select set_config('timezone','UTC',true);

do $$
begin
    if (select data_type from information_schema.columns
        where table_schema='leadgen' and table_name='leads' and column_name='payload')='text' then
        alter table leadgen.leads
            alter column first_seen type timestamptz using first_seen::timestamptz,
            alter column sent_at type timestamptz using nullif(btrim(sent_at),'')::timestamptz,
            alter column payload type jsonb using payload::jsonb,
            add column source text generated always as (payload->>'source') stored;
    end if;
end;
$$;

-- Trigram search over the lead title and text (leadgen.web.LEAD_SEARCH_TEXT). Supabase keeps extensions in the
-- `extensions` schema. The index itself is built CONCURRENTLY by 20261014150000_lead_search_indexes.sql; it names
-- extensions.gin_trgm_ops, so this transactional part fails (and rolls back) when pg_trgm lives in another schema
-- instead of letting the deploy stop halfway after the column types have changed.
create schema if not exists extensions;
create extension if not exists pg_trgm with schema extensions;

do $$
begin
    if not exists (select 1 from pg_opclass c join pg_namespace n on n.oid=c.opcnamespace
                   where c.opcname='gin_trgm_ops' and n.nspname='extensions') then
        raise exception 'pg_trgm is installed outside the extensions schema: ALTER EXTENSION pg_trgm SET SCHEMA extensions';
    end if;
end;
$$;
