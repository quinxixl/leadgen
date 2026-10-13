create table leadgen.ai_offer_generations (
    id bigint generated always as identity primary key,
    workspace_id bigint not null,
    owner_user_id bigint not null,
    lead_id bigint not null,
    project_id bigint,
    requested_by bigint not null references leadgen.app_users(id) on delete restrict,
    model text not null check(length(model) between 1 and 120),
    status text not null default 'pending' check(status in ('pending','completed','failed')),
    drafts jsonb not null default '{}'::jsonb check(jsonb_typeof(drafts)='object'),
    input_tokens integer not null default 0 check(input_tokens>=0),
    output_tokens integer not null default 0 check(output_tokens>=0),
    prompt_fingerprint text not null check(length(prompt_fingerprint)=64),
    error text not null default '' check(length(error)<=300),
    created_at timestamptz not null default now(),
    completed_at timestamptz,
    foreign key(workspace_id,owner_user_id)
        references leadgen.workspaces(id,owner_user_id) on delete cascade,
    foreign key(owner_user_id,lead_id)
        references leadgen.user_leads(user_id,lead_id) on delete cascade,
    foreign key(project_id) references leadgen.projects(id) on delete set null
);

create index ai_offer_generations_rate_limit
    on leadgen.ai_offer_generations(owner_user_id,created_at desc);
create index ai_offer_generations_latest
    on leadgen.ai_offer_generations(owner_user_id,lead_id,project_id,created_at desc)
    where status='completed';

alter table leadgen.ai_offer_generations enable row level security;
revoke all on leadgen.ai_offer_generations from public,anon,authenticated;
revoke all on all sequences in schema leadgen from public,anon,authenticated;
