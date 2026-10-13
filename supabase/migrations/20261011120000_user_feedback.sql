-- Product feedback from the bot, the web cabinet and the Mini App, plus beta survey answers.
create table if not exists leadgen.feedback (
    id bigint generated always as identity primary key,
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    workspace_id bigint references leadgen.workspaces(id) on delete set null,
    kind text not null check(kind in ('idea','bug','other','survey')),
    text text not null default '' check(length(text)<=4000),
    context text not null default '' check(length(context)<=200),
    source text not null check(source in ('bot','web','miniapp')),
    status text not null default 'new' check(status in ('new','seen','done')),
    -- Beta surveys (kind='survey'): which survey and the structured answers.
    survey text check(survey is null or survey in ('d7','d21')),
    disappointment text check(disappointment is null or disappointment in ('very','somewhat','not')),
    price text check(price is null or price in ('0','lt500','500_1000','1000_2000','gt2000')),
    answered_at timestamptz,
    notified_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check((kind='survey')=(survey is not null)),
    check(kind='survey' or length(btrim(text))>0)
);
create unique index if not exists feedback_one_survey on leadgen.feedback(user_id,survey) where survey is not null;
create index if not exists feedback_recent on leadgen.feedback(created_at desc);
create index if not exists feedback_unnotified on leadgen.feedback(created_at) where notified_at is null;

alter table leadgen.feedback enable row level security;
revoke all on leadgen.feedback from public,anon,authenticated;
