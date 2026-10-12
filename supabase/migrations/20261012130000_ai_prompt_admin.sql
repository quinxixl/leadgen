-- Admin-managed AI prompt versions (/admin/ai, platform admins only; users never see prompts).
-- Only the instructions text, the model, the temperature and a few numeric params are stored here.
-- The output contract (JSON schema, categories, allowed service names, field limits) stays in code
-- and is appended to every request, so an edited prompt cannot break parsing. Without an active
-- version the runtime falls back to the prompt constants in code (the «factory» version).
create table if not exists leadgen.ai_prompts (
    id bigint generated always as identity primary key,
    task text not null check (task in ('scoring','offers')),
    version integer not null check (version > 0),
    instructions text not null check (length(btrim(instructions)) > 0 and length(instructions) <= 8000),
    model text not null check (length(model) between 1 and 120),
    temperature numeric(3,2) not null default 0 check (temperature between 0 and 2),
    params jsonb not null default '{}'::jsonb check (jsonb_typeof(params) = 'object'),
    status text not null default 'draft' check (status in ('draft','active','archived')),
    note text not null default '' check (length(note) <= 500),
    -- Hash of the content that last passed the sandbox; activation compares it with the current content.
    content_hash text check (content_hash is null or content_hash ~ '^[0-9a-f]{64}$'),
    test_passed_at timestamptz,
    created_by bigint references leadgen.app_users(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    activated_at timestamptz,
    unique (task, version)
);
-- One live version per task; activation archives the previous one in the same transaction.
create unique index if not exists ai_prompts_one_active on leadgen.ai_prompts(task) where status = 'active';

alter table leadgen.ai_prompts enable row level security;
revoke all on leadgen.ai_prompts from public,anon,authenticated;

-- AI service detection: up to three names from the code list of services (core.TOPICS).
alter table leadgen.lead_ai_scores add column if not exists topics jsonb not null default '[]'::jsonb;
alter table leadgen.lead_ai_scores drop constraint if exists lead_ai_scores_topics_check;
alter table leadgen.lead_ai_scores add constraint lead_ai_scores_topics_check
    check (jsonb_typeof(topics) = 'array' and jsonb_array_length(topics) <= 3);
-- Prompt version that produced the score; NULL means the factory prompt.
alter table leadgen.lead_ai_scores add column if not exists prompt_id bigint
    references leadgen.ai_prompts(id) on delete set null;

-- Rescore from /admin/ai puts rows back to 'pending'; the scorer picks them up regardless of lead age.
alter table leadgen.lead_ai_scores drop constraint if exists lead_ai_scores_status_check;
alter table leadgen.lead_ai_scores add constraint lead_ai_scores_status_check
    check (status in ('pending','scored','skipped','failed'));
create index if not exists lead_ai_scores_pending on leadgen.lead_ai_scores(lead_id) where status = 'pending';
-- Daily limit and the admin journal count by the time a row was (re)scored.
create index if not exists lead_ai_scores_scored_at on leadgen.lead_ai_scores(updated_at) where status = 'scored';
create index if not exists lead_ai_scores_recent on leadgen.lead_ai_scores(updated_at desc);

-- Pausing AI scoring uses the leadgen.settings key 'ai:scoring_paused' (no schema change).
