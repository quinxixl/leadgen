-- leadgen:no-transaction
-- Indexes for the ON DELETE CASCADE foreign keys that reference leadgen.user_leads(user_id, lead_id).
-- Built CONCURRENTLY: no write lock on the tables (SHARE UPDATE EXCLUSIVE only).
--
-- Deleting user_leads rows (filter changes delete all of a user's 'filtered' rows; leadgen.filtered_cleanup deletes
-- old ones in batches) makes Postgres look up the referencing rows of every deleted row in each child table.
-- lead_activity, lead_reminders and lead_tags already have an index starting with (user_id, lead_id),
-- ai_offer_generations one starting with owner_user_id. These two did not, so each deleted row cost a sequential scan of the table:
--   lead_assignments: FK (owner_user_id, lead_id); indexes only (workspace_id, lead_id) PK, (assignee_user_id, ...),
--                     (notification_updated_at)
--   telegram_replies: FK (owner_id, lead_id); index (actor_id, owner_id, lead_id, created_at) starts with actor_id
CREATE INDEX CONCURRENTLY IF NOT EXISTS lead_assignments_owner_lead ON leadgen.lead_assignments(owner_user_id, lead_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS telegram_replies_owner_lead ON leadgen.telegram_replies(owner_id, lead_id);
