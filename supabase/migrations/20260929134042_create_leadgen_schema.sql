create schema if not exists leadgen;
revoke all on schema leadgen from public, anon, authenticated;

create table if not exists leadgen.leads (
    id bigint generated always as identity unique,
    url text primary key,
    fingerprint text not null,
    payload text not null,
    status text not null,
    reason text not null,
    first_seen text not null,
    sent_at text,
    attempts integer not null default 0,
    next_attempt double precision not null default 0
);
create index if not exists leads_fp on leadgen.leads(fingerprint);
create index if not exists leads_delivery_queue on leadgen.leads(status,next_attempt,first_seen);

create table if not exists leadgen.health (
    source text primary key,
    checked text,
    ok integer,
    count integer,
    error text
);

create table if not exists leadgen.telegram_sources (
    username text primary key,
    url text not null,
    title text not null,
    segment text,
    priority integer,
    selection_group text,
    original_members integer,
    members integer,
    online integer,
    status text not null default 'pending',
    enabled integer not null default 0,
    checked_at text,
    last_message_at text,
    check_reason text,
    last_message_id bigint not null default 0,
    metadata text not null default '{}'
);
create index if not exists telegram_sources_enabled
    on leadgen.telegram_sources(enabled,status,priority,online desc,members desc);

create table if not exists leadgen.settings (
    key text primary key,
    value text not null
);

create table if not exists leadgen.notification_recipients (
    chat_id bigint primary key,
    can_manage boolean not null default false,
    enabled boolean not null default true,
    created_at timestamptz not null default now()
);

alter table leadgen.leads enable row level security;
alter table leadgen.health enable row level security;
alter table leadgen.telegram_sources enable row level security;
alter table leadgen.settings enable row level security;
alter table leadgen.notification_recipients enable row level security;

revoke all on all tables in schema leadgen from public, anon, authenticated;
revoke all on all sequences in schema leadgen from public, anon, authenticated;
