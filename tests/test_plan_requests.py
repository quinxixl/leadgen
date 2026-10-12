"""Paid plan requests, manual plan management in /admin, the set-plan CLI and admin/service accounts."""
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import billing
from leadgen.database import db_open

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class PlanMath(unittest.TestCase):
    def test_quote_uses_annual_discount_and_founder_price(self):
        self.assertEqual(billing.quote('pro', 'month'), (1990, False))
        self.assertEqual(billing.quote('pro', 'year'), (1592 * 12, False))
        self.assertEqual(billing.quote('start', 'month', 690), (690, True))
        self.assertEqual(billing.quote('start', 'year', 690), (552 * 12, True))
        self.assertEqual(billing.quote('team', 'month', 690), (4990, False))
        for code, period in (('free', 'month'), ('pro', 'week'), ('vip', 'month')):
            with self.assertRaises(ValueError):billing.quote(code, period)

    def test_period_end_extends_same_running_plan(self):
        self.assertEqual(billing.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1).date().isoformat(), '2026-02-28')
        self.assertEqual(billing.add_months(datetime(2026, 11, 15, tzinfo=timezone.utc), 12).date().isoformat(), '2027-11-15')
        running = {'status': 'active', 'plan_code': 'pro', 'ends_at': NOW + timedelta(days=10)}
        self.assertEqual(billing.period_end(running, 'pro', 1, NOW), billing.add_months(NOW + timedelta(days=10), 1))
        self.assertEqual(billing.period_end(running, 'team', 1, NOW), billing.add_months(NOW, 1))
        self.assertEqual(billing.period_end(None, 'pro', 12, NOW), billing.add_months(NOW, 12))

    def test_admin_and_unlimited_ids_get_service_team_plan(self):
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '10, 11', 'UNLIMITED_TELEGRAM_IDS': '20'}):
            self.assertTrue(all(billing.is_unlimited(value) for value in (10, '11', 20)))
            self.assertFalse(billing.is_unlimited(30))
            self.assertFalse(billing.is_unlimited(None))
        row = billing.service_subscription({'founder_price': 690, 'billing_period': None})
        self.assertTrue(billing.is_service(row))
        self.assertEqual(billing.effective_plan(row, NOW + timedelta(days=3650)), 'team')
        self.assertEqual(row['founder_price'], 690)


@unittest.skipUnless(fixtures.DSN, 'WEB_TEST_DATABASE_URL is not configured')
class PlanRequestsPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        with self.db:
            self.db.execute('DELETE FROM settings WHERE key LIKE ?', ('rl:%',))
            self.db.execute("UPDATE app_users SET username='seller',terms_accepted_at=now() WHERE id=?", (self.other,))
            self.db.execute("UPDATE app_users SET username='boss',terms_accepted_at=now() WHERE id=?", (self.u,))
            self.db.execute('UPDATE subscriptions SET founder_price=690 WHERE user_id=?', (self.other,))
        self.sent = []
        self.app.config['TELEGRAM_SENDER'] = lambda method, payload: self.sent.append((method, payload))
        self.env = patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '', 'UNLIMITED_TELEGRAM_IDS': '',
                                           'FEEDBACK_CHAT_ID': '777', 'WEB_PUBLIC_URL': 'https://signalid.test'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def as_other(self):
        with self.client.session_transaction() as state:state.update(user_id=self.other, workspace_id=self.other_ws)

    def request(self, plan='pro', period='year', path='/app/billing/request', client=None, csrf='test-csrf'):
        return (client or self.client).post(path, data={'csrf': csrf, 'plan': plan, 'period': period})

    def requests_of(self, uid):
        return self.db.execute('SELECT * FROM plan_requests WHERE user_id=? ORDER BY id', (uid,)).fetchall()

    def subscription(self, uid):
        return self.db.execute('SELECT * FROM subscriptions WHERE user_id=?', (uid,)).fetchone()

    # --- requests ------------------------------------------------------------------------------
    def test_request_is_saved_notified_shown_and_updated(self):
        self.as_other()
        page = self.client.get('/app/billing').text
        self.assertIn('action="/app/billing/request"', page)
        self.assertIn('год −20%', page)
        self.assertNotIn('href="https://t.me/', page.split('<h2>Тарифы</h2>')[1])
        response = self.request('pro', 'year', client=self.client)
        self.assertEqual(response.status_code, 302)
        rows = self.requests_of(self.other)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['plan_code'], rows[0]['billing_period'], rows[0]['price'], rows[0]['status']),
                         ('pro', 'year', 1592 * 12, 'new'))
        self.assertEqual(len(self.sent), 1)
        method, payload = self.sent[0]
        self.assertEqual((method, payload['chat_id']), ('sendMessage', '777'))
        for part in (f'id {self.other}', '@seller', 'Второй', 'Telegram ID 2', '«Профи», год', '19 104 ₽',
                     'https://signalid.test/admin#plan-requests'):
            self.assertIn(part, payload['text'])
        page = self.client.get('/app/billing').text
        self.assertIn('мы свяжемся с вами в Telegram в течение дня', page)
        self.assertIn('Заявка отправлена', page)
        self.assertIn('Обновить заявку', page)
        # A second click updates the same open request.
        self.request('start', 'month')
        rows = self.requests_of(self.other)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['plan_code'], rows[0]['price'], rows[0]['founder_price']), ('start', 690, True))
        self.assertIn('обновлена', self.sent[-1][1]['text'])
        self.assertIn('цена участника беты', self.sent[-1][1]['text'])
        for bad in (('free', 'month'), ('pro', 'week'), ('vip', 'year')):
            self.assertEqual(self.request(*bad).status_code, 400, bad)
        self.assertEqual(self.client.post('/app/billing/request', data={'plan': 'pro', 'period': 'month'}).status_code, 400)

    def test_request_rate_limited(self):
        self.as_other()
        for _ in range(5):self.assertEqual(self.request('pro', 'month').status_code, 302)
        response = self.request('team', 'month', client=self.client)
        self.assertIn('Слишком много попыток', self.client.get('/app/billing').text)
        self.assertEqual(response.status_code, 302)
        self.assertEqual([row['plan_code'] for row in self.requests_of(self.other)], ['pro'])
        self.assertEqual(len(self.sent), 5)

    def test_notification_failure_keeps_request(self):
        def broken(method, payload):raise RuntimeError('Telegram: down')
        self.app.config['TELEGRAM_SENDER'] = broken
        self.as_other()
        with patch('leadgen.plan_requests.capture_exception') as capture:
            self.assertEqual(self.request('pro', 'month').status_code, 302)
        capture.assert_called_once()
        self.assertEqual(capture.call_args.kwargs['component'], 'plan_requests')
        self.assertEqual(len(self.requests_of(self.other)), 1)

    def test_team_member_cannot_request_for_owner(self):
        with self.db:
            self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'manager')", (self.ws, self.other))
        with self.client.session_transaction() as state:state.update(user_id=self.other, workspace_id=self.ws)
        self.request('pro', 'month')
        self.assertEqual(self.requests_of(self.other), [])
        self.assertEqual(self.requests_of(self.u), [])
        self.assertIn('Тариф выбирает владелец команды', self.client.get('/app/billing').text)

    def test_mini_app_request(self):
        tg = self.app.test_client()
        with tg.session_transaction('/tg/feed') as state:state.update(user_id=self.other, csrf='tg-csrf')
        page = tg.get('/tg/billing')
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/tg/billing/request"', page.text)
        self.assertIn('Подключить «Профи»', page.text)
        response = self.request('team', 'year', path='/tg/billing/request', client=tg, csrf='tg-csrf')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/tg/billing')
        self.assertEqual(self.requests_of(self.other)[0]['plan_code'], 'team')
        self.assertIn('мы свяжемся с вами в Telegram в течение дня', tg.get('/tg/billing').text)

    # --- admin ---------------------------------------------------------------------------------
    def test_non_admin_cannot_use_admin_actions(self):
        from leadgen.plan_requests import save_request
        row, _ = save_request(self.db, self.other, 'pro', 'month')
        self.assertEqual(self.client.get('/admin').status_code, 403)
        for path, data in (('/admin/plans', {'user': '@seller', 'plan': 'team', 'term': 'forever'}),
                           (f"/admin/plan-requests/{row['id']}/grant", {'period': 'month'}),
                           (f"/admin/plan-requests/{row['id']}/reject", {})):
            self.assertEqual(self.client.post(path, data={'csrf': 'test-csrf'} | data).status_code, 403, path)
        self.assertEqual(self.requests_of(self.other)[0]['status'], 'new')
        self.assertEqual(self.subscription(self.other)['plan_code'], 'trial')
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1'}):
            # CSRF is required even for admins.
            self.assertEqual(self.client.post('/admin/plans', data={'user': '@seller', 'plan': 'team', 'term': 'forever'}).status_code, 400)

    def test_admin_grants_and_rejects_requests(self):
        from leadgen.plan_requests import save_request
        row, _ = save_request(self.db, self.other, 'pro', 'month')
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1'}):
            page = self.client.get('/admin').text
            self.assertIn('id="plan-requests"', page)
            self.assertIn('@seller', page)
            response = self.client.post(f"/admin/plan-requests/{row['id']}/grant", data={'csrf': 'test-csrf', 'period': 'month'})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.client.post(f"/admin/plan-requests/{row['id']}/grant", data={'csrf': 'test-csrf'}).status_code, 404)
        sub = self.subscription(self.other)
        self.assertEqual((sub['status'], sub['plan_code'], sub['billing_period'], sub['provider']), ('active', 'pro', 'month', 'manual'))
        expected = billing.add_months(datetime.now(timezone.utc), 1)
        self.assertLess(abs((sub['ends_at'] - expected).total_seconds()), 120)
        handled = self.requests_of(self.other)[0]
        self.assertEqual((handled['status'], handled['handled_by']), ('done', self.u))
        self.assertIsNotNone(handled['handled_at'])
        self.assertEqual(self.sent[-1][1]['chat_id'], '2')
        self.assertIn('Тариф «Профи» подключён до ' + expected.astimezone(billing.MSK).strftime('%d.%m.%Y'), self.sent[-1][1]['text'])
        change = self.db.execute('SELECT * FROM plan_changes WHERE user_id=?', (self.other,)).fetchone()
        self.assertEqual((change['source'], change['changed_by'], change['old_plan'], change['new_plan']), ('request', self.u, 'trial', 'pro'))
        # A yearly grant on the same plan extends the running period.
        second, _ = save_request(self.db, self.other, 'pro', 'year')
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1'}):
            self.client.post(f"/admin/plan-requests/{second['id']}/grant", data={'csrf': 'test-csrf'})
            third, _ = save_request(self.db, self.other, 'team', 'month')
            self.assertEqual(self.client.post(f"/admin/plan-requests/{third['id']}/reject", data={'csrf': 'test-csrf'}).status_code, 302)
            self.assertEqual(self.client.post(f"/admin/plan-requests/{third['id']}/reject", data={'csrf': 'test-csrf'}).status_code, 404)
        self.assertEqual(self.subscription(self.other)['ends_at'], billing.add_months(sub['ends_at'], 12))
        self.assertEqual(self.subscription(self.other)['billing_period'], 'year')
        self.assertEqual([r['status'] for r in self.requests_of(self.other)], ['done', 'done', 'rejected'])
        self.assertEqual(self.subscription(self.other)['plan_code'], 'pro')

    def test_admin_sets_plan_term_and_founder_price(self):
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1'}):
            post = lambda **data: self.client.post('/admin/plans', data={'csrf': 'test-csrf', 'founder': 'keep'} | data)
            self.assertEqual(post(user='@Seller', plan='team', term='forever', founder='off').status_code, 302)
            sub = self.subscription(self.other)
            self.assertEqual((sub['plan_code'], sub['ends_at'], sub['founder_price']), ('team', None, None))
            post(user='2', plan='start', term='beta', founder='on')
            sub = self.subscription(self.other)
            self.assertEqual((sub['plan_code'], sub['ends_at'], sub['founder_price']), ('start', billing.beta_ends_at(), 690))
            post(user=str(self.other), plan='pro', term='date', until='2027-03-01')
            sub = self.subscription(self.other)
            self.assertEqual((sub['plan_code'], sub['ends_at'].astimezone(billing.MSK).isoformat()),
                             ('pro', '2027-03-01T00:00:00+03:00'))
            self.assertEqual(sub['founder_price'], 690)
            post(user='@seller', plan='pro', term='year')
            self.assertEqual(self.subscription(self.other)['ends_at'], billing.add_months(sub['ends_at'], 12))
            post(user='@seller', plan='free', term='month')
            self.assertEqual((self.subscription(self.other)['plan_code'], self.subscription(self.other)['ends_at']), ('free', None))
            response = post(user='@nobody', plan='pro', term='month')
            self.assertEqual(response.status_code, 302)
            self.assertIn('Пользователь не найден', self.client.get('/admin').text)
            self.assertEqual(post(user='@seller', plan='vip', term='month').status_code, 400)
            self.assertEqual(post(user='@seller', plan='pro', term='date', until='').status_code, 302)
            page = self.client.get('/admin').text
            self.assertIn('Последние изменения', page)
        changes = self.db.execute('SELECT source,changed_by FROM plan_changes WHERE user_id=?', (self.other,)).fetchall()
        self.assertEqual(len(changes), 5)
        self.assertTrue(all(row['source'] == 'admin' and row['changed_by'] == self.u for row in changes))

    def test_cli_set_plan(self):
        factory = lambda: db_open(fixtures.DSN)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(billing.main(['set-plan', '--user', '@seller', '--plan', 'pro', '--months', '1'], db_factory=factory), 0)
        self.assertIn('тариф «Профи» до', out.getvalue())
        sub = self.subscription(self.other)
        self.assertEqual((sub['plan_code'], sub['status']), ('pro', 'active'))
        self.assertLess(abs((sub['ends_at'] - billing.add_months(datetime.now(timezone.utc), 1)).total_seconds()), 120)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(billing.main(['set-plan', '--user', '2', '--plan', 'team', '--forever', '--founder', 'off'],
                                          db_factory=factory), 0)
            self.assertEqual(billing.main(['set-plan', '--user', '@ghost', '--plan', 'team', '--forever'], db_factory=factory), 1)
            with self.assertRaises(SystemExit):billing.main(['set-plan', '--user', '@seller', '--plan', 'pro'], db_factory=factory)
        sub = self.subscription(self.other)
        self.assertEqual((sub['plan_code'], sub['ends_at'], sub['founder_price']), ('team', None, None))
        sources = [row['source'] for row in self.db.execute('SELECT source FROM plan_changes WHERE user_id=?', (self.other,))]
        self.assertEqual(sources, ['cli', 'cli'])

    # --- admin and service accounts --------------------------------------------------------------
    def test_admin_and_unlimited_accounts_use_team_plan_everywhere(self):
        from leadgen.ai_offers import daily_limit
        from leadgen.product import RECIPIENTS, recipient_limits
        with self.db:
            self.db.execute("UPDATE subscriptions SET status='active',plan_code='free',ends_at=NULL")
            self.db.execute('UPDATE user_preferences SET monitoring_active=true')
        self.assertEqual(billing.owner_plan(self.db, self.u), 'free')
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1', 'UNLIMITED_TELEGRAM_IDS': '2'}):
            for uid in (self.u, self.other):
                self.assertEqual(billing.owner_plan(self.db, uid), 'team')
                self.assertEqual(daily_limit(self.db, uid), (100, 'team'))
                prefs = self.db.execute(RECIPIENTS + ' AND u.id=?', (uid,)).fetchone()
                self.assertEqual(recipient_limits(prefs), billing.plan_limits('team'))
                self.assertEqual(recipient_limits(prefs)['delay_minutes'], 0)
            # The stored row is untouched.
            self.assertEqual(self.subscription(self.u)['plan_code'], 'free')
            page = self.client.get('/app/billing').text
            self.assertIn('Команда (служебный)', page)
            self.assertIn('Без ограничения срока', page)
            for i in range(3):
                self.client.post('/app/projects', data={'csrf': 'test-csrf', 'name': f'Проект {i}'})
            count = self.db.execute('SELECT count(*) AS n FROM projects WHERE user_id=?', (self.u,)).fetchone()['n']
            self.assertEqual(count, 3)
            self.client.post('/app/team/invite', data={'csrf': 'test-csrf', 'role': 'manager'})
            self.assertTrue(self.db.execute('SELECT 1 FROM team_invites WHERE workspace_id=?', (self.ws,)).fetchone())
            self.assertIn('Команда (служебный)', self.client.get('/admin').text)
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '', 'UNLIMITED_TELEGRAM_IDS': ''}):
            self.assertEqual(billing.owner_plan(self.db, self.other), 'free')
            prefs = self.db.execute(RECIPIENTS + ' AND u.id=?', (self.other,)).fetchone()
            self.assertEqual(recipient_limits(prefs)['delay_minutes'], billing.FREE_LEAD_DELAY_MINUTES)

    def test_bot_subscription_text_for_service_account(self):
        from leadgen.product import ProductController
        bot = ProductController.__new__(ProductController)
        bot.db = self.db
        with patch.dict(os.environ, {'UNLIMITED_TELEGRAM_IDS': '2'}):
            text = bot._subscription_text(bot._user(2))
        self.assertIn('«Команда» (служебный)', text)
        self.assertIn('AI-откликов в день: 100', text)


if __name__ == '__main__':
    unittest.main()
