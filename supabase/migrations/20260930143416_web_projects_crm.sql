create table leadgen.projects (
 id bigint generated always as identity primary key,
 user_id bigint not null references leadgen.app_users(id) on delete cascade,
 name text not null check (length(name) between 1 and 100),
 description text not null default '',
 keywords jsonb not null default '[]',
 stop_words jsonb not null default '[]',
 topics jsonb not null default '[]',
 source_names jsonb not null default '[]',
 min_budget integer not null default 5000 check(min_budget between 0 and 100000000),
 min_score integer not null default 0 check(min_score between 0 and 100),
 show_without_budget boolean not null default false,
 show_possible_needs boolean not null default true,
 enabled boolean not null default true,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(id,user_id)
);
create index projects_owner on leadgen.projects(user_id,enabled);
create table leadgen.project_leads (
 project_id bigint not null,
 user_id bigint not null,
 lead_id bigint not null references leadgen.leads(id) on delete cascade,
 matched_at timestamptz not null default now(),
 primary key(project_id,lead_id),
 foreign key(project_id,user_id) references leadgen.projects(id,user_id) on delete cascade
);
create index project_leads_owner on leadgen.project_leads(user_id,lead_id);
create table leadgen.lead_activity (
 id bigint generated always as identity primary key,
 user_id bigint not null,
 lead_id bigint not null,
 kind text not null check(kind in ('status','note','feedback','amount')),
 detail text not null check(length(detail)<=4000),
 created_at timestamptz not null default now(),
 foreign key(user_id,lead_id) references leadgen.user_leads(user_id,lead_id) on delete cascade
);
create index lead_activity_owner on leadgen.lead_activity(user_id,lead_id,created_at desc);
alter table leadgen.user_leads drop constraint user_leads_pipeline_status_check;
alter table leadgen.user_leads add constraint user_leads_pipeline_status_check
 check(pipeline_status in ('saved','viewed','working','contacted','discussing','meeting','proposal','won','lost','not_fit'));
alter table leadgen.projects enable row level security;
alter table leadgen.project_leads enable row level security;
alter table leadgen.lead_activity enable row level security;
revoke all on leadgen.projects,leadgen.project_leads,leadgen.lead_activity from public,anon,authenticated;
revoke all on all sequences in schema leadgen from public,anon,authenticated;
