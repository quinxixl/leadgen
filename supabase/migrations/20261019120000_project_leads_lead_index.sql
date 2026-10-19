-- leadgen:no-transaction
-- Index for the ON DELETE CASCADE foreign key project_leads(lead_id) -> leads(id), built CONCURRENTLY (no write lock).
--
-- leadgen.filtered_cleanup.cleanup_rejected deletes old rejected/duplicate leads in batches. For every deleted lead
-- Postgres looks up the referencing rows of each child table: user_leads (user_leads_lead_id_idx), lead_feedback
-- (lead_feedback_lead_id_idx) and lead_ai_scores (primary key) are indexed on lead_id; project_leads only had
-- (project_id, lead_id) and (user_id, lead_id), so each deleted lead cost a sequential scan of project_leads.
-- The same index serves the cleanup's NOT EXISTS (project_leads.lead_id = leads.id) check.
CREATE INDEX CONCURRENTLY IF NOT EXISTS project_leads_lead ON leadgen.project_leads(lead_id);
