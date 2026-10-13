alter table leadgen.projects
    add column reply_sender text not null default '' check(length(reply_sender)<=120),
    add column reply_offer text not null default '' check(length(reply_offer)<=1000),
    add column reply_proof jsonb not null default '[]'::jsonb,
    add column reply_question text not null default '' check(length(reply_question)<=300),
    add column reply_signature text not null default '' check(length(reply_signature)<=300),
    add column forbidden_phrases jsonb not null default '[]'::jsonb,
    add column reply_max_length smallint not null default 1000 check(reply_max_length between 200 and 1500);

alter table leadgen.projects
    add constraint projects_reply_proof_array check(jsonb_typeof(reply_proof)='array'),
    add constraint projects_forbidden_phrases_array check(jsonb_typeof(forbidden_phrases)='array');
