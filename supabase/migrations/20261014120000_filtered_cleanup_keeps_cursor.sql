-- Age-based cleanup of 'filtered' user_leads rows (leadgen.filtered_cleanup) must not reset delivery cursors.
--
-- The trigger from 20261013140000_user_delivery_cursor.sql sets delivery_cursor = 0 whenever a user's filtered rows
-- are deleted: a filter change deletes them so those leads are evaluated again under the new rules. The cleanup
-- deletes only rows of leads below the freshness-window floor (plus a 24 h margin). The delivery pass never scans
-- those leads again (since = max(cursor, floor)), so their markers carry no information and a reset would only make
-- every affected user re-classify the whole window on the next pass, hourly.
--
-- The cleanup marks its own transactions with set_config('leadgen.keep_delivery_cursor', 'on', true) (transaction
-- local, gone at commit/rollback). Every other DELETE of filtered rows (filter changes in web.py, web_projects.py,
-- ProductController._update_pref) keeps resetting the cursor exactly as before.
-- CREATE OR REPLACE FUNCTION only swaps the body: no table lock, the trigger itself is unchanged.
create or replace function leadgen.user_leads_reset_delivery_cursor() returns trigger
language plpgsql
set search_path = leadgen, pg_temp
as $$
begin
    if coalesce(current_setting('leadgen.keep_delivery_cursor', true), '') = 'on' then
        return null;
    end if;
    update leadgen.user_preferences p
       set delivery_cursor = 0, updated_at = now()
     where p.user_id in (select distinct o.user_id from old_rows o where o.delivery_status = 'filtered');
    return null;
end;
$$;

revoke all on function leadgen.user_leads_reset_delivery_cursor() from public, anon, authenticated;
