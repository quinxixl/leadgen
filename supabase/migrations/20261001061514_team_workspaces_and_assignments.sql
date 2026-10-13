create table leadgen.workspaces (
    id bigint generated always as identity primary key,
    owner_user_id bigint not null unique references leadgen.app_users(id) on delete cascade,
    name text not null check(length(name) between 1 and 100),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(id,owner_user_id)
);

create table leadgen.workspace_members (
    workspace_id bigint not null references leadgen.workspaces(id) on delete cascade,
    user_id bigint not null references leadgen.app_users(id) on delete cascade,
    role text not null check(role in ('owner','admin','manager','viewer')),
    joined_at timestamptz not null default now(),
    primary key(workspace_id,user_id)
);
create index workspace_members_user on leadgen.workspace_members(user_id,workspace_id);

insert into leadgen.workspaces(owner_user_id,name)
select id,left(coalesce(nullif(display_name,''),'Моя команда') || ' — команда',100)
from leadgen.app_users on conflict(owner_user_id) do nothing;
insert into leadgen.workspace_members(workspace_id,user_id,role)
select id,owner_user_id,'owner' from leadgen.workspaces on conflict do nothing;

alter table leadgen.projects add column workspace_id bigint references leadgen.workspaces(id) on delete cascade;
update leadgen.projects p set workspace_id=w.id from leadgen.workspaces w where w.owner_user_id=p.user_id;
alter table leadgen.projects alter column workspace_id set not null;
create index projects_workspace on leadgen.projects(workspace_id,enabled);

create table leadgen.team_invites (
    id bigint generated always as identity primary key,
    workspace_id bigint not null references leadgen.workspaces(id) on delete cascade,
    token_hash text not null unique check(length(token_hash)=64),
    role text not null check(role in ('admin','manager','viewer')),
    invited_by bigint not null references leadgen.app_users(id) on delete cascade,
    expires_at timestamptz not null,
    accepted_by bigint references leadgen.app_users(id) on delete set null,
    accepted_at timestamptz,
    revoked boolean not null default false,
    created_at timestamptz not null default now()
);
create index team_invites_workspace on leadgen.team_invites(workspace_id,expires_at desc);

create table leadgen.lead_assignments (
    workspace_id bigint not null,
    owner_user_id bigint not null,
    lead_id bigint not null,
    assignee_user_id bigint not null,
    assigned_by bigint not null references leadgen.app_users(id) on delete restrict,
    assigned_at timestamptz not null default now(),
    primary key(workspace_id,lead_id),
    foreign key(workspace_id,owner_user_id) references leadgen.workspaces(id,owner_user_id) on delete cascade,
    foreign key(owner_user_id,lead_id) references leadgen.user_leads(user_id,lead_id) on delete cascade,
    foreign key(workspace_id,assignee_user_id) references leadgen.workspace_members(workspace_id,user_id) on delete cascade
);
create index lead_assignments_assignee on leadgen.lead_assignments(assignee_user_id,assigned_at desc);

alter table leadgen.lead_activity drop constraint lead_activity_kind_check;
alter table leadgen.lead_activity add constraint lead_activity_kind_check
    check(kind in ('status','note','feedback','amount','assignment'));

alter table leadgen.workspaces enable row level security;
alter table leadgen.workspace_members enable row level security;
alter table leadgen.team_invites enable row level security;
alter table leadgen.lead_assignments enable row level security;
revoke all on leadgen.workspaces,leadgen.workspace_members,leadgen.team_invites,
    leadgen.lead_assignments from public,anon,authenticated;
revoke all on all sequences in schema leadgen from public,anon,authenticated;
