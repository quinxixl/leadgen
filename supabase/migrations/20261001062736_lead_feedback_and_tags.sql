alter table leadgen.lead_feedback drop constraint lead_feedback_label_check;
alter table leadgen.lead_feedback add constraint lead_feedback_label_check
    check(label in ('fit','ad','job','not_service','too_cold','wrong_geo','competitor','vacancy','duplicate'));

create table leadgen.lead_tags (
    user_id bigint not null,
    lead_id bigint not null,
    tag text not null check(length(tag) between 1 and 40),
    created_at timestamptz not null default now(),
    primary key(user_id,lead_id,tag),
    foreign key(user_id,lead_id) references leadgen.user_leads(user_id,lead_id) on delete cascade
);
create index lead_tags_owner_tag on leadgen.lead_tags(user_id,tag,lead_id);
alter table leadgen.lead_tags enable row level security;
revoke all on leadgen.lead_tags from public,anon,authenticated;
