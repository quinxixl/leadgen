create table leadgen.api_tokens (
    id bigint generated always as identity primary key,
    workspace_id bigint not null references leadgen.workspaces(id) on delete cascade,
    name text not null check(length(name) between 1 and 100),
    token_hash text not null unique check(length(token_hash)=64),
    access text not null check(access in ('read','write')),
    last_used_at timestamptz,
    revoked boolean not null default false,
    created_by bigint not null references leadgen.app_users(id) on delete restrict,
    created_at timestamptz not null default now()
);
create index api_tokens_workspace on leadgen.api_tokens(workspace_id,revoked);

create table leadgen.webhooks (
    id bigint generated always as identity primary key,
    workspace_id bigint not null references leadgen.workspaces(id) on delete cascade,
    name text not null check(length(name) between 1 and 100),
    url text not null check(length(url) between 10 and 1000),
    secret_cipher text not null,
    active boolean not null default true,
    created_by bigint not null references leadgen.app_users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index webhooks_workspace on leadgen.webhooks(workspace_id,active);

create table leadgen.webhook_outbox (
    id bigint generated always as identity primary key,
    webhook_id bigint not null references leadgen.webhooks(id) on delete cascade,
    event_type text not null check(event_type in ('lead.created','lead.updated')),
    event_key text not null check(length(event_key) between 1 and 200),
    payload jsonb not null,
    status text not null default 'pending' check(status in ('pending','processing','sent','failed','dead')),
    attempts smallint not null default 0 check(attempts between 0 and 10),
    next_attempt_at timestamptz not null default now(),
    last_error text not null default '',
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    unique(webhook_id,event_key)
);
create index webhook_outbox_ready on leadgen.webhook_outbox(next_attempt_at,id)
    where status in ('pending','failed');

alter table leadgen.api_tokens enable row level security;
alter table leadgen.webhooks enable row level security;
alter table leadgen.webhook_outbox enable row level security;
revoke all on leadgen.api_tokens,leadgen.webhooks,leadgen.webhook_outbox from public,anon,authenticated;
revoke all on all sequences in schema leadgen from public,anon,authenticated;
