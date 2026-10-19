-- Exact row count of every table in schema leadgen, one "table|rows" line each, sorted by name.
-- Run the same file against Supabase and the local db and diff the outputs (DEPLOY.md).
SELECT c.relname || '|' || (xpath('/row/n/text()',
       query_to_xml(format('SELECT count(*) AS n FROM leadgen.%I', c.relname), false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'leadgen' AND c.relkind IN ('r', 'p')
ORDER BY c.relname;
