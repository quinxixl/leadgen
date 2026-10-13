alter table leadgen.lead_assignments
    add column notification_status text not null default 'pending'
        check(notification_status in ('pending','sending','sent','uncertain')),
    add column notification_error text not null default '',
    add column notification_updated_at timestamptz not null default now();
create index lead_assignments_notification on leadgen.lead_assignments(notification_updated_at)
    where notification_status='pending';
