create table leadgen.lead_reminders (
 user_id bigint not null,
 lead_id bigint not null,
 due_at timestamptz not null,
 note text not null default '' check(length(note)<=500),
 status text not null default 'pending' check(status in ('pending','sending','sent','uncertain','cancelled')),
 updated_at timestamptz not null default now(),
 primary key(user_id,lead_id),
 foreign key(user_id,lead_id) references leadgen.user_leads(user_id,lead_id) on delete cascade
);
create index lead_reminders_due on leadgen.lead_reminders(due_at) where status='pending';
alter table leadgen.lead_reminders enable row level security;
revoke all on leadgen.lead_reminders from public,anon,authenticated;
