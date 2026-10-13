-- Per-user delivery cursor for leadgen.product.deliver_registered.
-- Low-water mark: every lead with id <= delivery_cursor is settled for the user (recorded in user_leads or
-- excluded for good), so each delivery pass scans only leads.id > delivery_cursor instead of walking the
-- user's whole history backwards through leads_id_key.
-- 0 means "start from the freshness window" (max_age_hours + 2 h): classify() rejects every older lead anyway.
-- Adding a column with a constant default is metadata-only (no table rewrite); the table has one row per user.
alter table leadgen.user_preferences
    add column if not exists delivery_cursor bigint not null default 0;

-- Changing filters deletes the user's 'filtered' rows so those leads are evaluated again under the new rules
-- (leadgen.web.save_preferences, leadgen.web_projects, ProductController._update_pref). Those leads may lie below
-- the cursor, so the cursor goes back to 0. updated_at changes too: the worker advances the cursor only with a
-- compare-and-set on (delivery_cursor, updated_at) read at the start of its pass, so a pass that raced with the
-- reset cannot move the cursor past the leads it is about to re-evaluate.
create or replace function leadgen.user_leads_reset_delivery_cursor() returns trigger
language plpgsql
set search_path = leadgen, pg_temp
as $$
begin
    update leadgen.user_preferences p
       set delivery_cursor = 0, updated_at = now()
     where p.user_id in (select distinct o.user_id from old_rows o where o.delivery_status = 'filtered');
    return null;
end;
$$;

revoke all on function leadgen.user_leads_reset_delivery_cursor() from public, anon, authenticated;

drop trigger if exists user_leads_reset_delivery_cursor on leadgen.user_leads;
create trigger user_leads_reset_delivery_cursor
    after delete on leadgen.user_leads
    referencing old table as old_rows
    for each statement execute function leadgen.user_leads_reset_delivery_cursor();
