alter table leadgen.leads
    add column origin_user_id bigint references leadgen.app_users(id) on delete cascade;
create index leads_origin_user on leadgen.leads(origin_user_id,id desc);

create table leadgen.telegram_connections (
    user_id bigint primary key references leadgen.app_users(id) on delete cascade,
    telegram_user_id bigint unique,
    display_name text not null default '',
    phone_hint text not null default '',
    session_cipher text not null default '',
    status text not null default 'pending'
        check (status in ('pending','active','error','revoked')),
    last_connected_at timestamptz,
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table leadgen.telegram_connection_auth (
    user_id bigint primary key references leadgen.app_users(id) on delete cascade,
    state_cipher text not null,
    stage text not null check (stage in ('code','password')),
    attempts smallint not null default 0 check (attempts between 0 and 5),
    expires_at timestamptz not null,
    updated_at timestamptz not null default now()
);

create table leadgen.user_telegram_dialogs (
    user_id bigint not null references leadgen.telegram_connections(user_id) on delete cascade,
    peer_id bigint not null,
    title text not null check (length(title) between 1 and 300),
    username text,
    kind text not null check (kind in ('group','supergroup')),
    enabled boolean not null default false,
    last_message_at timestamptz,
    updated_at timestamptz not null default now(),
    primary key(user_id,peer_id)
);
create index user_telegram_dialogs_enabled
    on leadgen.user_telegram_dialogs(user_id,enabled,peer_id);

alter table leadgen.telegram_connections enable row level security;
alter table leadgen.telegram_connection_auth enable row level security;
alter table leadgen.user_telegram_dialogs enable row level security;
revoke all on leadgen.telegram_connections,leadgen.telegram_connection_auth,
    leadgen.user_telegram_dialogs from public,anon,authenticated;
