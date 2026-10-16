-- leadgen:no-transaction
-- Indexes over the typed lead columns from 20261014140000_typed_lead_columns.sql. Built CONCURRENTLY: no write lock.

-- Source filter of the leads page and the AI admin journal (l.source=?).
CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_source ON leadgen.leads(source);

-- Leads search: LEAD_SEARCH_TEXT ILIKE '%term%' in leadgen.web. The expression must stay identical to
-- leadgen.web.LEAD_SEARCH_TEXT. Terms shorter than 3 characters have no trigrams and fall back to a filter scan.
CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_search_trgm ON leadgen.leads
    USING gin ((coalesce(payload->>'title','') || ' ' || coalesce(payload->>'text','')) extensions.gin_trgm_ops);
