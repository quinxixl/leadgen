-- leadgen:no-transaction
-- Index for the ingest duplicate lookup (leadgen.app.find_duplicate), built CONCURRENTLY: no write lock on leads.
--
--   SELECT url FROM leads WHERE fingerprint=? AND url<>? AND status IN ('ready','sent','sending','uncertain')
--     AND origin_user_id IS NULL | =?  LIMIT 1
--
-- pg_stat_statements 2026-10-18: ~958k calls, 114 ms mean. leads_fp(fingerprint) walks every row of a fingerprint
-- (reposts of one message across hundreds of chats share it) and fetches each heap row to test status, while
-- nearly all of them are rejected/review/duplicate. The partial index holds only the live rows the lookup can match.
--
-- Works on the old text-payload schema too (fingerprint, status and origin_user_id are unchanged), so it may be
-- created ahead of time from the SQL editor with exactly this statement; this migration is then a no-op.
-- The predicate must stay identical to leadgen.app.LIVE_STATUSES.

CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_fp_live ON leadgen.leads(fingerprint, origin_user_id)
    WHERE status IN ('ready','sent','sending','uncertain');
