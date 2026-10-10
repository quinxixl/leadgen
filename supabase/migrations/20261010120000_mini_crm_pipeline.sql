-- Mini-CRM: a short pipeline (new → replied → in_progress → won / lost, plus archived),
-- a deal note and timestamps for "earned this month" and reply→win conversion.
-- Legacy statuses are mapped in place; the API and old bot buttons keep accepting them (leadgen/crm.py).
alter table leadgen.user_leads drop constraint user_leads_pipeline_status_check;

alter table leadgen.user_leads
    add column crm_note text not null default '' check (length(crm_note) <= 1000),
    add column replied_at timestamptz,
    add column won_at timestamptz,
    add column status_changed_at timestamptz;

-- First reply: the earliest status entry that means "we wrote to the client" or later.
update leadgen.user_leads ul set replied_at = coalesce(
    (select min(a.created_at) from leadgen.lead_activity a
     where a.user_id = ul.user_id and a.lead_id = ul.lead_id and a.kind = 'status'
       and a.detail in ('Написали','Получен ответ','Назначена встреча','Отправлено предложение','Получил заказ')),
    case when ul.pipeline_status in ('contacted','discussing','meeting','proposal','won') then ul.updated_at end);

update leadgen.user_leads ul set won_at = coalesce(
    (select max(a.created_at) from leadgen.lead_activity a
     where a.user_id = ul.user_id and a.lead_id = ul.lead_id and a.kind = 'status' and a.detail = 'Получил заказ'),
    ul.updated_at)
where ul.pipeline_status = 'won';

update leadgen.user_leads set status_changed_at = updated_at;

update leadgen.user_leads set pipeline_status = case pipeline_status
    when 'saved' then 'new' when 'viewed' then 'new'
    when 'contacted' then 'replied'
    when 'working' then 'in_progress' when 'discussing' then 'in_progress'
    when 'meeting' then 'in_progress' when 'proposal' then 'in_progress'
    when 'not_fit' then 'archived'
    else pipeline_status end
where pipeline_status in ('saved','viewed','contacted','working','discussing','meeting','proposal','not_fit');

alter table leadgen.user_leads alter column pipeline_status set default 'new';
alter table leadgen.user_leads add constraint user_leads_pipeline_status_check
    check (pipeline_status in ('new','replied','in_progress','won','lost','archived'));

-- Every writer (cabinet, Mini App, bot, API, Telegram replies) gets the stamps without extra code.
create or replace function leadgen.user_leads_crm_stamps() returns trigger
language plpgsql set search_path = leadgen, pg_temp as $$
begin
    if new.pipeline_status is distinct from old.pipeline_status then
        new.status_changed_at := now();
        if new.pipeline_status in ('replied','in_progress','won') and new.replied_at is null then
            new.replied_at := now();
        end if;
        if new.pipeline_status = 'won' then
            new.won_at := now();
        elsif old.pipeline_status = 'won' then
            new.won_at := null;
        end if;
    end if;
    return new;
end $$;
revoke all on function leadgen.user_leads_crm_stamps() from public, anon, authenticated;

create trigger user_leads_crm_stamps before update of pipeline_status on leadgen.user_leads
    for each row execute function leadgen.user_leads_crm_stamps();

create index user_leads_won on leadgen.user_leads(user_id, won_at) where pipeline_status = 'won';
