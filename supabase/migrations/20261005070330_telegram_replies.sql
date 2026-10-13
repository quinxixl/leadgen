-- Additive: old bot/web releases ignore this table; no existing data changes.
create table leadgen.telegram_replies (
    id text primary key check (id ~ '^[0-9a-f]{32}$'),
    actor_id bigint not null references leadgen.app_users(id) on delete cascade,
    owner_id bigint not null,
    lead_id bigint not null,
    mode text not null check (mode in ('dm','group')),
    body text not null check (length(body) between 1 and 3000),
    source_url text not null,
    target_cipher text not null,
    sender_label text not null,
    recipient_label text not null,
    account_id bigint not null,
    random_id bigint not null,
    status text not null default 'draft'
        check (status in ('draft','sending','sent','failed','uncertain','cancelled')),
    error text not null default '',
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    foreign key (owner_id,lead_id) references leadgen.user_leads(user_id,lead_id) on delete cascade,
    unique (account_id,random_id)
);
create index telegram_replies_history on leadgen.telegram_replies(actor_id,owner_id,lead_id,created_at desc);
alter table leadgen.telegram_replies enable row level security;
revoke all on leadgen.telegram_replies from public,anon,authenticated;
