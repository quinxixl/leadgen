create index if not exists lead_feedback_lead_id_idx on leadgen.lead_feedback(lead_id);
create index if not exists user_chat_sources_username_idx on leadgen.user_chat_sources(username);
create index if not exists user_leads_lead_id_idx on leadgen.user_leads(lead_id);

revoke execute on function public.rls_auto_enable() from public,anon,authenticated;
