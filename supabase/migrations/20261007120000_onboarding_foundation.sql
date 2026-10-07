alter table leadgen.app_users add column if not exists terms_accepted_at timestamptz;
alter table leadgen.app_users add column if not exists session_version integer not null default 0;
alter table leadgen.user_preferences alter column topics set default '[]'::jsonb;
