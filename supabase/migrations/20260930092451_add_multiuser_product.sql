create table if not exists leadgen.app_users (
    id bigint generated always as identity primary key,
    telegram_user_id bigint not null unique,
    telegram_chat_id bigint not null unique,
    username text,
    display_name text not null default '',
    status text not null default 'active' check (status in ('active','blocked')),
    registered_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now()
);

create table if not exists leadgen.subscriptions (
    id bigint generated always as identity primary key,
    user_id bigint not null unique references leadgen.app_users(id) on delete cascade,
    plan_code text not null default 'stub',
    status text not null default 'stub_active'
        check (status in ('stub_active','active','past_due','canceled')),
    provider text not null default 'stub',
    starts_at timestamptz not null default now(),
    ends_at timestamptz,
    updated_at timestamptz not null default now()
);

create table if not exists leadgen.user_preferences (
    user_id bigint primary key references leadgen.app_users(id) on delete cascade,
    monitoring_active boolean not null default false,
    min_budget integer not null default 5000 check (min_budget between 0 and 100000000),
    show_without_budget boolean not null default false,
    show_possible_needs boolean not null default true,
    topics jsonb not null default '["Сайты","Боты","Мобильные приложения","Автоматизации","CRM"]'::jsonb,
    source_switches jsonb not null default '{}'::jsonb,
    profile_services text not null default '',
    portfolio text not null default '',
    state jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists leadgen.user_chat_sources (
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    username text not null references leadgen.telegram_sources(username) on delete cascade,
    enabled boolean not null default true,
    created_at timestamptz not null default now(),
    primary key (user_id,username)
);

create table if not exists leadgen.user_leads (
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    lead_id bigint not null references leadgen.leads(id) on delete cascade,
    delivery_status text not null default 'pending'
        check (delivery_status in ('pending','sent','filtered','uncertain')),
    pipeline_status text not null default 'saved'
        check (pipeline_status in ('saved','contacted','discussing','won','lost','not_fit')),
    filter_reason text not null default '',
    deal_amount integer check (deal_amount is null or deal_amount >= 0),
    sent_at timestamptz,
    updated_at timestamptz not null default now(),
    primary key (user_id,lead_id)
);
create index if not exists user_leads_pipeline on leadgen.user_leads(user_id,pipeline_status,updated_at desc);
create index if not exists user_leads_delivery on leadgen.user_leads(user_id,delivery_status,updated_at desc);

create table if not exists leadgen.lead_feedback (
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    lead_id bigint not null references leadgen.leads(id) on delete cascade,
    label text not null check (label in ('fit','ad','job','not_service')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id,lead_id)
);
create index if not exists lead_feedback_quality on leadgen.lead_feedback(label,lead_id);

insert into leadgen.app_users (telegram_user_id,telegram_chat_id,display_name)
select chat_id,chat_id,case when can_manage then 'Владелец' else 'Получатель уведомлений' end
from leadgen.notification_recipients
where enabled=true
on conflict (telegram_user_id) do nothing;

insert into leadgen.subscriptions (user_id)
select id from leadgen.app_users
on conflict (user_id) do nothing;

insert into leadgen.user_preferences (user_id,monitoring_active)
select id,true from leadgen.app_users
on conflict (user_id) do nothing;

alter table leadgen.app_users enable row level security;
alter table leadgen.subscriptions enable row level security;
alter table leadgen.user_preferences enable row level security;
alter table leadgen.user_chat_sources enable row level security;
alter table leadgen.user_leads enable row level security;
alter table leadgen.lead_feedback enable row level security;

revoke all on leadgen.app_users,leadgen.subscriptions,leadgen.user_preferences,
    leadgen.user_chat_sources,leadgen.user_leads,leadgen.lead_feedback
    from public,anon,authenticated;
revoke all on all sequences in schema leadgen from public,anon,authenticated;
