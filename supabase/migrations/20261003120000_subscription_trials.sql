-- Paid product: new accounts start with a 7-day trial. Existing early-access
-- accounts keep stub_active until they are moved to a paid plan.
alter table leadgen.subscriptions drop constraint if exists subscriptions_status_check;
alter table leadgen.subscriptions add constraint subscriptions_status_check
    check (status in ('stub_active','trial','active','past_due','canceled'));
alter table leadgen.subscriptions alter column status set default 'trial';
alter table leadgen.subscriptions alter column plan_code set default 'trial';
alter table leadgen.subscriptions alter column ends_at set default (now() + interval '7 days');
