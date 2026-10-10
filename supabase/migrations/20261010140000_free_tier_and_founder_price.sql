-- Pricing v2: after the trial an account falls back to the Free plan instead of losing access,
-- subscriptions record the billing period (month or year) and the beta founder price lock.
alter table leadgen.subscriptions add column if not exists billing_period text not null default 'month';
alter table leadgen.subscriptions drop constraint if exists subscriptions_billing_period_check;
alter table leadgen.subscriptions add constraint subscriptions_billing_period_check
    check (billing_period in ('month','year'));

-- Monthly price of «Старт» locked for a beta founder, in rubles; null means the regular price.
alter table leadgen.subscriptions add column if not exists founder_price integer;
alter table leadgen.subscriptions drop constraint if exists subscriptions_founder_price_check;
alter table leadgen.subscriptions add constraint subscriptions_founder_price_check
    check (founder_price is null or founder_price between 0 and 1000000);

-- Everyone registered so far joined during the beta.
update leadgen.subscriptions set founder_price=690 where founder_price is null;

-- Early-access accounts had no end date. They get a fresh 7-day trial with full limits,
-- then fall back to Free like everyone else (with the founder price available).
update leadgen.subscriptions
   set status='trial',plan_code='trial',ends_at=now()+interval '7 days',updated_at=now()
 where status='stub_active';

-- Ended trials, ended paid periods and canceled or unpaid subscriptions continue on Free.
update leadgen.subscriptions
   set status='active',plan_code='free',ends_at=null,billing_period='month',updated_at=now()
 where status in ('past_due','canceled')
    or (status in ('trial','active') and ends_at is not null and ends_at<=now());
