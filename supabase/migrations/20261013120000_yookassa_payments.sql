-- YooKassa payments: one-off checkout with proration, saved payment method and autorenewal.
-- Activation happens only from a verified notification (or a server-side status check);
-- the manual plan-request flow keeps working when YooKassa is not configured.

-- E-mail for 54-ФЗ receipts: asked once at checkout and prefilled next time.
alter table leadgen.app_users add column if not exists email text;
alter table leadgen.app_users drop constraint if exists app_users_email_check;
alter table leadgen.app_users add constraint app_users_email_check
    check (email is null or (length(email) between 3 and 254 and email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'));

-- Autorenewal state of the owner's subscription. Only the YooKassa payment method id and its
-- masked title (e.g. «Bank card *4444») are stored, never other card data.
alter table leadgen.subscriptions add column if not exists payment_method_id text;
alter table leadgen.subscriptions add column if not exists payment_method_title text;
alter table leadgen.subscriptions add column if not exists autorenew boolean not null default false;
alter table leadgen.subscriptions add column if not exists autorenew_consent_at timestamptz;
-- A cheaper plan or year→month is applied at the next renewal.
alter table leadgen.subscriptions add column if not exists scheduled_plan text;
alter table leadgen.subscriptions add column if not exists scheduled_period text;
-- After a failed renewal the paid plan keeps working until grace_until (status past_due).
alter table leadgen.subscriptions add column if not exists grace_until timestamptz;
alter table leadgen.subscriptions drop constraint if exists subscriptions_scheduled_plan_check;
alter table leadgen.subscriptions add constraint subscriptions_scheduled_plan_check
    check ((scheduled_plan is null and scheduled_period is null)
        or (scheduled_plan in ('start','pro','team') and scheduled_period in ('month','year')));
alter table leadgen.subscriptions drop constraint if exists subscriptions_status_check;
alter table leadgen.subscriptions add constraint subscriptions_status_check
    check (status in ('stub_active','trial','active','past_due','canceled'));

alter table leadgen.plan_changes drop constraint if exists plan_changes_source_check;
alter table leadgen.plan_changes add constraint plan_changes_source_check
    check (source in ('admin','request','cli','payment'));

create table if not exists leadgen.payments (
    id uuid primary key,
    -- Workspace owner whose subscription the payment applies to, and who started it.
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    created_by bigint references leadgen.app_users(id) on delete set null,
    yookassa_payment_id text unique,
    kind text not null check (kind in ('initial','upgrade','renewal')),
    plan_code text not null check (plan_code in ('start','pro','team')),
    billing_period text not null check (billing_period in ('month','year')),
    -- Rubles with kopecks: amount charged, credit for the unused part of the current paid period.
    amount numeric(12,2) not null check (amount > 0),
    credit numeric(12,2) not null default 0 check (credit >= 0),
    status text not null default 'pending' check (status in ('pending','succeeded','canceled','failed')),
    -- Renewals cover a known period from creation; one-off payments get it on activation.
    period_start timestamptz,
    period_end timestamptz,
    payment_method_id text,
    confirmation_url text,
    cancellation_party text,
    cancellation_reason text,
    email text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    paid_at timestamptz
);
create index if not exists payments_user on leadgen.payments(user_id,created_at desc);
create index if not exists payments_pending on leadgen.payments(created_at) where status='pending';
-- Never two live or successful renewals of the same period: failed attempts do not block a retry.
create unique index if not exists payments_one_renewal on leadgen.payments(user_id,period_start)
    where kind='renewal' and status in ('pending','succeeded');

create table if not exists leadgen.payment_refunds (
    id uuid primary key,
    payment_id uuid not null references leadgen.payments(id) on delete cascade,
    yookassa_refund_id text unique,
    amount numeric(12,2) not null check (amount > 0),
    status text not null default 'pending' check (status in ('pending','succeeded','canceled','failed')),
    created_by bigint references leadgen.app_users(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists payment_refunds_payment on leadgen.payment_refunds(payment_id);

alter table leadgen.payments enable row level security;
alter table leadgen.payment_refunds enable row level security;
revoke all on leadgen.payments,leadgen.payment_refunds from public,anon,authenticated;
