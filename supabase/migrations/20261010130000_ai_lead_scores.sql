-- Shared AI assessment of a lead: computed once per lead and reused by every user.
create table if not exists leadgen.lead_ai_scores (
    lead_id bigint primary key references leadgen.leads(id) on delete cascade,
    status text not null check(status in ('scored','skipped','failed')),
    score smallint check(score is null or score between 1 and 10),
    category text check(category is null or category in ('service_request','ad','job','spam','other')),
    budget_amount numeric(14,2) check(budget_amount is null or budget_amount>=0),
    budget_currency text check(budget_currency is null or budget_currency ~ '^[A-Z]{3}$'),
    urgency text check(urgency is null or urgency in ('low','medium','high')),
    summary text not null default '' check(length(summary)<=200),
    model text not null default '' check(length(model)<=120),
    input_tokens integer not null default 0 check(input_tokens>=0),
    output_tokens integer not null default 0 check(output_tokens>=0),
    attempts integer not null default 0 check(attempts>=0),
    error text not null default '' check(length(error)<=300),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check(status<>'scored' or (score is not null and category is not null and urgency is not null))
);
create index if not exists lead_ai_scores_retry on leadgen.lead_ai_scores(status,updated_at) where status='failed';
create index if not exists lead_ai_scores_daily on leadgen.lead_ai_scores(created_at) where status='scored';

alter table leadgen.lead_ai_scores enable row level security;
revoke all on leadgen.lead_ai_scores from public,anon,authenticated;

-- Per-user AI filters: hide ads/vacancies/spam by default; 0 disables the minimum score.
alter table leadgen.user_preferences add column if not exists ai_hide_noise boolean not null default true;
alter table leadgen.user_preferences add column if not exists ai_min_score smallint not null default 0
    check(ai_min_score between 0 and 10);

-- Tuning dataset: user feedback next to the shared AI verdict.
create or replace view leadgen.lead_ai_feedback as
    select f.user_id,f.lead_id,f.label,f.updated_at AS feedback_at,
           s.status,s.score,s.category,s.urgency,s.model
    from leadgen.lead_feedback f join leadgen.lead_ai_scores s on s.lead_id=f.lead_id;
revoke all on leadgen.lead_ai_feedback from public,anon,authenticated;
