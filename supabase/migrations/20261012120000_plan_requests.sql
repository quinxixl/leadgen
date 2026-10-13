-- Payments are not integrated yet: a user asks for a paid plan from «Подписка» (cabinet or Mini App),
-- the owner is notified in Telegram and grants the plan by hand in /admin or with the CLI.
create table if not exists leadgen.plan_requests (
    id bigint generated always as identity primary key,
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    plan_code text not null check (plan_code in ('start','pro','team')),
    billing_period text not null check (billing_period in ('month','year')),
    -- Quoted amount for the whole period in rubles; founder_price marks the beta founder «Старт» price.
    price integer not null check (price between 0 and 10000000),
    founder_price boolean not null default false,
    status text not null default 'new' check (status in ('new','done','rejected')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    handled_at timestamptz,
    handled_by bigint references leadgen.app_users(id) on delete set null
);
-- One open request per user: a repeated click updates it.
create unique index if not exists plan_requests_one_open on leadgen.plan_requests(user_id) where status='new';
create index if not exists plan_requests_recent on leadgen.plan_requests(created_at desc);

-- Audit of manual plan changes (admin page, request grants and the CLI).
create table if not exists leadgen.plan_changes (
    id bigint generated always as identity primary key,
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    changed_by bigint references leadgen.app_users(id) on delete set null,
    source text not null check (source in ('admin','request','cli')),
    old_plan text,
    old_ends_at timestamptz,
    new_plan text not null,
    new_ends_at timestamptz,
    billing_period text not null default 'month',
    founder_price integer,
    note text not null default '' check (length(note)<=500),
    created_at timestamptz not null default now()
);
create index if not exists plan_changes_user on leadgen.plan_changes(user_id,created_at desc);

alter table leadgen.plan_requests enable row level security;
alter table leadgen.plan_changes enable row level security;
revoke all on leadgen.plan_requests,leadgen.plan_changes from public,anon,authenticated;
