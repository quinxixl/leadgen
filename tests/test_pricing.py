"""Plans and quotas: Free fallback after the trial, plan limits, Free lead delay, API and webhooks switched off."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import billing
from leadgen.web import create_app

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class Plans(unittest.TestCase):
    def test_prices_limits_and_annual_discount(self):
        plans = {plan['code']: plan for plan in billing.PLANS}
        self.assertEqual([plan['code'] for plan in billing.PLANS], ['free', 'start', 'pro', 'team'])
        self.assertEqual({code: plan['price'] for code, plan in plans.items()},
                         {'free': 0, 'start': 990, 'pro': 1990, 'team': 4990})
        self.assertEqual({code: (plan['limits']['projects'], plan['limits']['members'], plan['limits']['drafts'])
                          for code, plan in plans.items()},
                         {'free': (1, 1, 3), 'start': (1, 1, 10), 'pro': (5, 1, 30), 'team': (20, 10, 100)})
        self.assertEqual(plans['free']['limits']['delay_minutes'], billing.FREE_LEAD_DELAY_MINUTES)
        self.assertTrue(all(plans[code]['limits']['delay_minutes'] == 0 for code in ('start', 'pro', 'team')))
        self.assertEqual(plans['start']['annual_month_price'], 792)
        self.assertEqual(plans['start']['annual_price'], 9504)
        self.assertEqual(plans['team']['annual_price'], 4990 * 12 * 80 // 100)
        self.assertEqual(billing.FOUNDER_START_PRICE, 690)
        self.assertFalse(any('API' in item or 'webhook' in item for plan in billing.PLANS for item in plan['features']))

    def test_trial_and_paid_periods_fall_back_to_free(self):
        future, past = NOW + timedelta(days=1), NOW - timedelta(seconds=1)
        cases = [({'status': 'trial', 'plan_code': 'trial', 'ends_at': future}, 'trial'),
                 ({'status': 'trial', 'plan_code': 'trial', 'ends_at': past}, 'free'),
                 ({'status': 'active', 'plan_code': 'pro', 'ends_at': future}, 'pro'),
                 ({'status': 'active', 'plan_code': 'team', 'ends_at': past}, 'free'),
                 ({'status': 'canceled', 'plan_code': 'start', 'ends_at': None}, 'free'),
                 ({'status': 'active', 'plan_code': 'free', 'ends_at': None}, 'free'),
                 ({'status': 'active', 'plan_code': 'unknown', 'ends_at': None}, 'free'),
                 (None, 'free')]
        for row, expected in cases:
            with self.subTest(row=row):self.assertEqual(billing.effective_plan(row, NOW), expected)
        self.assertEqual(billing.plan_limits('trial'), billing.plan_limits('pro'))

    def test_early_access_gets_free_start_until_beta_end(self):
        end = billing.beta_ends_at()
        self.assertEqual(end, datetime(2026, 11, 15, tzinfo=billing.MSK))
        row = {'status': 'active', 'plan_code': 'start', 'ends_at': end}
        self.assertEqual(billing.effective_plan(row, NOW), 'start')
        self.assertEqual(billing.beta_label(row, NOW), 'Старт бесплатно до 15.11.2026')
        after = end + timedelta(seconds=1)
        self.assertEqual(billing.effective_plan(row, after), 'free')
        self.assertIsNone(billing.beta_label(row, after))
        paid = {'status': 'active', 'plan_code': 'start', 'ends_at': end + timedelta(days=3)}
        self.assertIsNone(billing.beta_label(paid, NOW))
        with patch.dict(os.environ, {'BETA_ENDS_AT': '2026-12-01'}):
            self.assertEqual(billing.beta_ends_at().date().isoformat(), '2026-12-01')
            self.assertIsNone(billing.beta_label(row, NOW))
        with patch.dict(os.environ, {'BETA_ENDS_AT': 'soon'}):
            self.assertEqual(billing.beta_ends_at(), end)

    def test_limit_messages_name_the_plan_that_lifts_the_quota(self):
        self.assertIn('«Бесплатный» доступно проектов: 1', billing.limit_message('projects', 'free'))
        self.assertIn('«Профи» — до 5', billing.limit_message('projects', 'free'))
        self.assertIn('работа в команде недоступна', billing.limit_message('members', 'pro'))
        self.assertIn('«Команда» — до 10', billing.limit_message('members', 'start'))
        self.assertIn('«Старт» — до 10', billing.limit_message('drafts', 'free'))
        self.assertNotIn('до', billing.limit_message('projects', 'team').split('.')[1])

    def test_delay_clause_only_for_delayed_plans(self):
        self.assertEqual(billing.delay_clause(0), ('', []))
        sql, args = billing.delay_clause(60)
        self.assertIn('l.first_seen::timestamptz <= now()', sql);self.assertEqual(args, [60])


class PublicPricing(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-' * 10, 'BOT_USERNAME': 'test_bot',
                               'SESSION_COOKIE_SECURE': False})
        self.client = self.app.test_client()

    def test_pricing_and_landing_show_free_tier_without_api(self):
        for path in ('/pricing', '/'):
            page = self.client.get(path).text
            for text in ('Бесплатный', '990 ₽', '1 990 ₽', '4 990 ₽', 'при оплате за год', 'Начать бесплатно'):
                self.assertIn(text, page, (path, text))
            for text in ('1 490', '3 490', '7 990', 'API и webhook', 'Доступ приостановится'):
                self.assertNotIn(text, page, (path, text))
        self.assertIn('бесплатном тарифе', self.client.get('/terms').text)

    def test_api_and_integrations_disabled_by_default(self):
        with patch.dict(os.environ, {'INTEGRATIONS_ENABLED': ''}):
            app = create_app({'TESTING': True, 'SECRET_KEY': 'test-' * 10, 'SESSION_COOKIE_SECURE': False})
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertFalse({'/api/v1/leads', '/app/integrations'} & rules)
        client = app.test_client()
        response = client.get('/api/v1/leads', headers={'Authorization': 'Bearer lf_test'})
        self.assertEqual(response.status_code, 404);self.assertEqual(response.get_json()['status'], 404)
        self.assertEqual(client.patch('/api/v1/leads/1', json={'status': 'won'}).status_code, 404)

    def test_integrations_flag_restores_routes(self):
        from leadgen.integrations import integrations_enabled
        with patch.dict(os.environ, {'INTEGRATIONS_ENABLED': '1'}):
            self.assertTrue(integrations_enabled())
            app = create_app({'TESTING': True, 'SECRET_KEY': 'test-' * 10, 'SESSION_COOKIE_SECURE': False})
        self.assertIn('/api/v1/leads', {rule.rule for rule in app.url_map.iter_rules()})
        with patch.dict(os.environ, {'INTEGRATIONS_ENABLED': '0'}):self.assertFalse(integrations_enabled())


@unittest.skipUnless(fixtures.DSN, 'WEB_TEST_DATABASE_URL is not configured')
class PricingPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)

    def plan(self, user, code, status='active', ends="NULL"):
        with self.db:self.db.execute(f"UPDATE subscriptions SET status=?,plan_code=?,ends_at={ends} WHERE user_id=?",
                                     (status, code, user))

    def expire_trial(self, user):
        with self.db:self.db.execute("UPDATE subscriptions SET ends_at=now()-interval '1 minute' WHERE user_id=?", (user,))

    # --- quotas ----------------------------------------------------------------------------------
    def test_project_quota_follows_plan(self):
        self.expire_trial(self.u)
        self.assertEqual(self.client.post('/app/projects', data={'csrf': 'test-csrf', 'name': 'Первый'}).status_code, 302)
        response = self.client.post('/app/projects', data={'csrf': 'test-csrf', 'name': 'Второй'}, follow_redirects=True)
        self.assertIn('На тарифе «Бесплатный» доступно проектов: 1', response.text)
        self.assertIn('Проектов: 1 из 1', response.text)
        count = lambda: self.db.execute('SELECT count(*) AS n FROM projects WHERE user_id=?', (self.u,)).fetchone()['n']
        self.assertEqual(count(), 1)
        self.plan(self.u, 'pro')
        for i in range(4):self.client.post('/app/projects', data={'csrf': 'test-csrf', 'name': f'Проект {i}'})
        self.assertEqual(count(), 5)
        self.client.post('/app/projects', data={'csrf': 'test-csrf', 'name': 'Шестой'})
        self.assertEqual(count(), 5)

    def test_downgrade_keeps_only_oldest_projects_in_delivery(self):
        from leadgen.product import active_projects
        with self.db:
            for name in ('А', 'Б', 'В'):
                self.db.execute('INSERT INTO projects(user_id,workspace_id,name,enabled) VALUES(?,?,?,true)', (self.u, self.ws, name))
        self.assertEqual([row['name'] for row in active_projects(self.db, self.u, billing.plan_limits('free'))], ['А'])
        self.assertEqual(len(active_projects(self.db, self.u, billing.plan_limits('pro'))), 3)

    def test_member_quota_blocks_invites_and_joins(self):
        response = self.client.post('/app/team/invite', data={'csrf': 'test-csrf', 'role': 'manager'}, follow_redirects=True)
        self.assertIn('работа в команде недоступна', response.text)
        self.assertIn('Участников: 1 из 1', response.text)
        self.assertFalse(self.db.execute('SELECT 1 FROM team_invites WHERE workspace_id=?', (self.ws,)).fetchone())
        self.plan(self.u, 'team')
        self.client.post('/app/team/invite', data={'csrf': 'test-csrf', 'role': 'manager'})
        with self.client.session_transaction() as state:invite_path = state['new_invite_path']
        # The owner moves to a plan without seats before the invite is used.
        self.plan(self.u, 'start')
        with self.client.session_transaction() as state:state['user_id'] = self.other;state['workspace_id'] = self.other_ws
        response = self.client.post(invite_path, data={'csrf': 'test-csrf'})
        self.assertEqual(response.status_code, 200);self.assertIn('нет свободных мест', response.text)
        self.assertFalse(self.db.execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',
                                         (self.ws, self.other)).fetchone())
        self.plan(self.u, 'team')
        self.assertEqual(self.client.post(invite_path, data={'csrf': 'test-csrf'}).status_code, 302)
        self.assertTrue(self.db.execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',
                                        (self.ws, self.other)).fetchone())

    def test_ai_draft_quota_follows_plan(self):
        from leadgen.ai_offers import AIOfferError, generate_for_lead
        from leadgen.core import Lead
        self.expire_trial(self.u)
        with self.db:
            for _ in range(3):
                self.db.execute('''INSERT INTO ai_offer_generations(workspace_id,owner_user_id,lead_id,requested_by,model,
                    prompt_fingerprint,status,created_at) VALUES(?,?,?,?,'m',repeat('f',64),'completed',now()-interval '1 hour')''',
                    (self.ws, self.u, self.ids[0], self.u))
        payload = json.loads(self.db.execute('SELECT payload FROM leads WHERE id=?', (self.ids[0],)).fetchone()['payload'])
        lead = Lead(**payload)
        with patch.dict(os.environ, {'GROQ_API_KEY': 'test-key'}):
            with self.assertRaises(AIOfferError) as error:
                generate_for_lead(self.db, self.ws, self.u, self.u, self.ids[0], lead, generator=fixtures.fake_ai_generator)
            self.assertIn('доступно AI-откликов в день: 3', str(error.exception))
            self.assertIn('«Старт» — до 10', str(error.exception))
            self.plan(self.u, 'start')
            drafts, _, _ = generate_for_lead(self.db, self.ws, self.u, self.u, self.ids[0], lead,
                                             generator=fixtures.fake_ai_generator)
            self.assertTrue(drafts)

    # --- trial fallback and Free delay -----------------------------------------------------------
    def test_free_plan_delays_bot_delivery_and_web_list(self):
        from leadgen.product import deliver_registered
        config = {'sources': [], 'max_age_hours': 72, 'min_budget': 0}
        with self.db:
            self.db.execute('DELETE FROM user_leads WHERE user_id=?', (self.u,))
            self.db.execute('UPDATE app_users SET registered_at=now()-interval \'1 day\' WHERE id=?', (self.u,))
            self.db.execute('UPDATE user_preferences SET monitoring_active=true,show_without_budget=true WHERE user_id=?', (self.u,))
            self.db.execute("UPDATE leads SET first_seen=(now()-interval '10 minutes')::text,origin_user_id=NULL")
        self.expire_trial(self.u)
        calls = []
        send = lambda *args:calls.append(args)
        self.assertEqual(deliver_registered(self.db, config, send, limit_per_user=10), 0)
        self.assertFalse([call for call in calls if call[1]['chat_id'] == '1'])
        # Paid plans get the same lead at once.
        self.plan(self.u, 'start')
        self.assertGreaterEqual(deliver_registered(self.db, config, send, limit_per_user=10), 1)
        self.assertTrue([call for call in calls if call[1]['chat_id'] == '1'])
        # Back on Free, the delivered lead stays hidden in the cabinet until the delay passes.
        self.plan(self.u, 'free')
        self.assertNotIn('Нужен сайт 0', self.client.get('/app/leads').text)
        with self.db:self.db.execute("UPDATE leads SET first_seen=(now()-interval '61 minutes')::text")
        self.assertIn('Нужен сайт 0', self.client.get('/app/leads').text)

    def test_free_plan_delivers_after_delay(self):
        from leadgen.product import deliver_registered
        with self.db:
            self.db.execute('DELETE FROM user_leads WHERE user_id=?', (self.u,))
            self.db.execute('UPDATE app_users SET registered_at=now()-interval \'1 day\' WHERE id=?', (self.u,))
            self.db.execute('UPDATE user_preferences SET monitoring_active=true,show_without_budget=true WHERE user_id=?', (self.u,))
            self.db.execute("UPDATE leads SET first_seen=(now()-interval '61 minutes')::text")
        self.plan(self.u, 'free')
        calls = []
        deliver_registered(self.db, {'sources': [], 'max_age_hours': 72, 'min_budget': 0}, lambda *args:calls.append(args), 10)
        self.assertTrue([call for call in calls if call[1]['chat_id'] == '1'])

    def test_registration_grants_founder_price_while_beta_is_open(self):
        from leadgen.accounts import register_user
        uid = register_user(self.db, {'id': 777, 'first_name': 'Бета'}, 777)
        self.assertEqual(self.db.execute('SELECT founder_price FROM subscriptions WHERE user_id=?', (uid,)).fetchone()['founder_price'], 690)
        with patch.dict(os.environ, {'BETA_FOUNDER_OPEN': '0'}):
            late = register_user(self.db, {'id': 778, 'first_name': 'После'}, 778)
        self.assertIsNone(self.db.execute('SELECT founder_price FROM subscriptions WHERE user_id=?', (late,)).fetchone()['founder_price'])
        with self.db:self.db.execute('UPDATE subscriptions SET founder_price=690 WHERE user_id=?', (self.u,))
        self.assertIn('Для вас как участника беты', self.client.get('/app/billing').text)
        self.assertIn('Для вас как участника беты', self.client.get('/pricing').text)

    def test_migration_moves_existing_subscriptions_sensibly(self):
        with self.db:
            self.db.execute("UPDATE subscriptions SET status='stub_active',plan_code='stub',ends_at=NULL,founder_price=NULL WHERE user_id=?", (self.u,))
            self.db.execute("UPDATE subscriptions SET status='trial',plan_code='trial',ends_at=now()-interval '1 day',founder_price=NULL WHERE user_id=?", (self.other,))
            third = self.db.execute("INSERT INTO app_users(telegram_user_id,telegram_chat_id,display_name) VALUES(3,3,'Третий') RETURNING id").fetchone()['id']
            self.db.execute("INSERT INTO subscriptions(user_id,status,plan_code,ends_at) VALUES(?,'active','pro',now()+interval '10 days')", (third,))
            fourth = self.db.execute("INSERT INTO app_users(telegram_user_id,telegram_chat_id,display_name) VALUES(4,4,'Четвёртый') RETURNING id").fetchone()['id']
            self.db.execute("INSERT INTO subscriptions(user_id,status,plan_code,ends_at) VALUES(?,'canceled','start',NULL)", (fourth,))
        self.db.connection.execute(Path('supabase/migrations/20261010140000_free_tier_and_founder_price.sql').read_text(), prepare=False)
        self.db.commit()
        rows = {row['user_id']: row for row in self.db.execute(
            'SELECT user_id,status,plan_code,ends_at>now()+interval \'6 days\' AS fresh,ends_at,founder_price FROM subscriptions')}
        if datetime.now(timezone.utc) < billing.beta_ends_at():
            self.assertEqual((rows[self.u]['status'], rows[self.u]['plan_code'], rows[self.u]['ends_at']),
                             ('active', 'start', billing.beta_ends_at()))
            self.assertIn('Старт бесплатно до 15.11.2026', self.client.get('/app/billing').text)
        else:
            self.assertEqual((rows[self.u]['status'], rows[self.u]['plan_code']), ('active', 'free'))
        self.assertEqual((rows[self.other]['status'], rows[self.other]['plan_code'], rows[self.other]['ends_at']), ('active', 'free', None))
        self.assertEqual((rows[third]['status'], rows[third]['plan_code']), ('active', 'pro'))
        self.assertEqual((rows[fourth]['status'], rows[fourth]['plan_code']), ('active', 'free'))
        self.assertTrue(all(row['founder_price'] == 690 for row in rows.values()))
        self.assertEqual(self.db.execute("SELECT billing_period FROM subscriptions WHERE user_id=?", (third,)).fetchone()['billing_period'], 'month')

    def test_webhook_events_not_queued_while_disabled(self):
        from leadgen.integrations import deliver_webhooks, enqueue_lead_event
        with self.db:
            self.db.execute("INSERT INTO webhooks(workspace_id,name,url,secret_cipher,created_by) VALUES(?,'CRM','https://example.com/h','x',?)",
                            (self.ws, self.u))
        with patch.dict(os.environ, {'INTEGRATIONS_ENABLED': ''}):
            with self.db:self.assertEqual(enqueue_lead_event(self.db, self.u, self.ids[0], 'lead.updated'), 0)
            self.assertEqual(deliver_webhooks(self.db, 'unused'), 0)
        self.assertFalse(self.db.execute('SELECT 1 FROM webhook_outbox').fetchone())
        with patch.dict(os.environ, {'INTEGRATIONS_ENABLED': '1'}):
            with self.db:self.assertEqual(enqueue_lead_event(self.db, self.u, self.ids[0], 'lead.updated'), 1)
        with self.db:self.db.execute('DELETE FROM webhooks WHERE workspace_id=?', (self.ws,))
