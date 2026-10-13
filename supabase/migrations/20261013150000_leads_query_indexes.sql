-- leadgen:no-transaction
-- Indexes for the cursor-based delivery queries (leadgen.product). Built CONCURRENTLY: no write lock on the tables.
--
-- The per-user scan itself needs no new index: deliver_registered now walks leads_id_key only above the user's
-- delivery_cursor (Index Cond: id > cursor), and the window floor / settled horizon / max(id) lookups are short
-- backward walks of the same index. first_seen is text, its ::timestamptz cast is not immutable and cannot be indexed.

-- deliver_registered and backfill_first_leads, UNSEEN clause: "the user already got a lead with this fingerprint"
--   NOT EXISTS (SELECT 1 FROM user_leads seen JOIN leads original ON original.id=seen.lead_id
--               WHERE seen.user_id=? AND seen.delivery_status<>'filtered' AND original.fingerprint=l.fingerprint)
-- About 99.9% of user_leads rows are 'filtered' (2026-10-13: 256 899 filtered, 254 sent/uncertain), so when Postgres
-- plans this side as a hash (generic plans, larger windows) it bitmap-scans every row of the user to keep ~0.1%.
-- The partial index holds only the delivered rows: an index-only scan of a few hundred entries.
-- Also serves the cabinet statistics in leadgen.web (ul.user_id=? AND ul.delivery_status<>'filtered').
CREATE INDEX CONCURRENTLY IF NOT EXISTS user_leads_delivered ON leadgen.user_leads(user_id, lead_id) WHERE delivery_status <> 'filtered';
