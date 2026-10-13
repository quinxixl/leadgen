"""YooKassa payments: price and proration math, the HTTP client, notifications, checkout, renewals and refunds."""
import copy
import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import billing, payments, yookassa

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
D = Decimal


def sub(plan='pro', period='month', ends=None, provider='yookassa', status='active'):
    return {'status': status, 'plan_code': plan, 'billing_period': period, 'provider': provider,
            'ends_at': ends if ends is not None else NOW + timedelta(days=15), 'founder_price': None}


class PriceAndProration(unittest.TestCase):
    def test_price_uses_plans_founder_and_annual_discount(self):
        self.assertEqual(payments.plan_price('pro', 'month'), D('1990.00'))
        self.assertEqual(payments.plan_price('pro', 'year'), D(1592 * 12))
        self.assertEqual(payments.plan_price('start', 'month', 690), D('690.00'))
        self.assertEqual(payments.plan_price('start', 'year', 690), D(552 * 12))
        self.assertEqual(payments.plan_price('team', 'month', 690), D('4990.00'))
        with self.assertRaises(ValueError):payments.plan_price('free', 'month')

    def test_trial_free_and_manual_periods_pay_full_price_from_now(self):
        for current in (None, sub('trial', status='trial', provider='stub'), sub('free', ends=None, provider='manual'),
                        sub('pro', provider='manual'), sub('start', provider='yookassa', ends=NOW - timedelta(days=1))):
            result = payments.proration(current, 'pro', 'month', NOW, None)
            self.assertEqual((result['kind'], result['amount'], result['credit']), ('initial', D('1990.00'), D('0.00')), current)
            self.assertEqual(result['starts_at'], NOW)
            self.assertEqual(result['ends_at'], billing.add_months(NOW, 1))

    def test_upgrade_credits_the_unused_paid_part(self):
        start, end = NOW - timedelta(days=15), NOW + timedelta(days=15)
        paid = {'value': D('990.00'), 'period_start': start, 'period_end': end}
        result = payments.proration(sub('start', ends=end), 'pro', 'month', NOW, paid)
        self.assertEqual(result['kind'], 'upgrade')
        self.assertEqual(result['credit'], D('495.00'))
        self.assertEqual(result['amount'], D('1495.00'))
        self.assertEqual(result['ends_at'], billing.add_months(NOW, 1))
        # Rounded to kopecks.
        paid['value'] = D('1000.00')
        third = payments.proration(sub('start', ends=end), 'pro', 'month', start + (end - start) * 2 / 3, paid)
        self.assertEqual(third['credit'], D('333.33'))
        self.assertEqual(third['amount'] + third['credit'], D('1990.00'))
        # Not paid through YooKassa for this period: no credit.
        self.assertEqual(payments.proration(sub('start', ends=end), 'pro', 'month', NOW, None)['credit'], D('0.00'))
        other = paid | {'period_end': end + timedelta(days=1)}
        self.assertEqual(payments.proration(sub('start', ends=end), 'pro', 'month', NOW, other)['credit'], D('0.00'))

    def test_month_to_year_is_an_upgrade_and_minimum_charge(self):
        end = NOW + timedelta(days=29, hours=23)
        paid = {'value': D('4990.00'), 'period_start': end - timedelta(days=30), 'period_end': end}
        result = payments.proration(sub('team', ends=end), 'start', 'year', NOW, paid)
        self.assertEqual(result['kind'], 'upgrade')
        self.assertEqual(result['ends_at'], billing.add_months(NOW, 12))
        self.assertGreater(result['credit'], D('4900'))
        # A credit larger than the new price still charges the 1 ₽ minimum.
        big = {'value': D('99999.00'), 'period_start': end - timedelta(days=30), 'period_end': end}
        result = payments.proration(sub('pro', ends=end), 'team', 'month', NOW, big)
        self.assertEqual(result['amount'], D('1.00'))
        self.assertEqual(result['credit'], D('4989.00'))

    def test_downgrade_and_year_to_month_are_scheduled_and_same_is_rejected(self):
        end = NOW + timedelta(days=100)
        for current, plan, period in ((sub('pro', ends=end), 'start', 'month'), (sub('pro', 'year', ends=end), 'team', 'month'),
                                      (sub('pro', 'year', ends=end), 'start', 'year')):
            result = payments.proration(current, plan, period, NOW, None)
            self.assertEqual((result['kind'], result['amount'], result['starts_at']), ('downgrade', D('0.00'), end))
        self.assertEqual(payments.proration(sub('pro', ends=end), 'pro', 'month', NOW)['kind'], 'same')
        data = payments.preview_json(payments.proration(sub('pro', ends=end), 'start', 'month', NOW))
        self.assertEqual((data['amount'], data['kind'], data['currency']), ('0.00', 'downgrade', 'RUB'))
        self.assertIn('при следующем продлении', data['message'])

    def test_past_due_keeps_plan_until_grace(self):
        row = {'status': 'past_due', 'plan_code': 'pro', 'ends_at': NOW - timedelta(days=1),
               'grace_until': NOW + timedelta(days=2)}
        self.assertEqual(billing.effective_plan(row, NOW), 'pro')
        self.assertEqual(billing.effective_plan(row, NOW + timedelta(days=3)), 'free')
        self.assertEqual(billing.effective_plan(row | {'grace_until': None}, NOW), 'free')

    def test_receipt_and_body(self):
        with patch.dict(os.environ, {'YOOKASSA_VAT_CODE': '', 'YOOKASSA_TAX_SYSTEM_CODE': '2'}):
            data = payments.receipt('a@b.ru', 'Тариф', D('990'))
        self.assertEqual(data['items'][0]['vat_code'], 1)
        self.assertEqual(data['items'][0]['amount'], {'value': '990.00', 'currency': 'RUB'})
        self.assertEqual((data['tax_system_code'], data['customer']['email']), (2, 'a@b.ru'))
        with self.assertRaises(payments.PaymentError):payments.normalize_email('nope')


class Client(unittest.TestCase):
    def test_ip_allowlist(self):
        for ip in ('185.71.76.1', '185.71.77.31', '77.75.153.100', '77.75.156.11', '77.75.156.35', '77.75.154.200',
                   '2a02:5180::1', '::ffff:185.71.76.1'):
            self.assertTrue(yookassa.trusted_ip(ip), ip)
        for ip in ('185.71.76.32', '77.75.156.12', '127.0.0.1', '', 'garbage', None, '2a03::1'):
            self.assertFalse(yookassa.trusted_ip(ip), ip)

    def test_requests_carry_auth_and_idempotence_key(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self, *args):return False

        def opener(req, timeout):
            seen.append((req, timeout))
            return Response(json.dumps({'id': 'p1', 'status': 'pending'}).encode())
        client = yookassa.Client('shop', 'key', opener=opener)
        self.assertEqual(client.create_payment({'amount': 1}, 'key-1')['id'], 'p1')
        req, timeout = seen[0]
        self.assertEqual(req.get_header('Idempotence-key'), 'key-1')
        self.assertEqual(req.get_header('Authorization'), 'Basic c2hvcDprZXk=')
        self.assertEqual(req.full_url, 'https://api.yookassa.ru/v3/payments')
        self.assertEqual(timeout, yookassa.TIMEOUT)
        client.get_payment('p/1')
        self.assertEqual(seen[1][0].full_url, 'https://api.yookassa.ru/v3/payments/p%2F1')
        with self.assertRaises(ValueError):client.create_payment({}, '')

    def test_errors_are_typed_and_unknown_outcome_retried_with_same_key(self):
        calls = []

        def opener(req, timeout):
            calls.append(req.get_header('Idempotence-key'))
            code = 500 if len(calls) < 3 else 400
            raise urllib.error.HTTPError(req.full_url, code, 'x', {}, io.BytesIO(b'{"code":"invalid_request"}'))
        client = yookassa.Client('shop', 'key', opener=opener)
        with self.assertRaises(yookassa.YooKassaUnavailable):client.create_payment({}, 'k')
        self.assertEqual(calls, ['k', 'k'])
        with self.assertRaises(yookassa.YooKassaError) as ctx:client.create_payment({}, 'k2')
        self.assertNotIsInstance(ctx.exception, yookassa.YooKassaUnavailable)
        self.assertEqual(ctx.exception.code, 'invalid_request')
        with patch.dict(os.environ, {'YOOKASSA_SHOP_ID': '', 'YOOKASSA_SECRET_KEY': ''}):
            self.assertFalse(yookassa.enabled())
            with self.assertRaises(yookassa.YooKassaError):yookassa.Client()


class FakeKassa:
    """In-memory YooKassa: idempotent by key, objects change only through pay()/cancel()."""
    def __init__(self):
        self.objects, self.by_key, self.created, self.gets, self.refunds = {}, {}, [], [], []
        self.renewal = ('succeeded', None)
        self.unavailable = False

    def create_payment(self, body, key):
        self.created.append((body, key))
        if self.unavailable:raise yookassa.YooKassaUnavailable('down')
        if key in self.by_key:return copy.deepcopy(self.objects[self.by_key[key]])
        pid = f'yk-{len(self.objects) + 1}'
        obj = {'id': pid, 'status': 'pending', 'amount': body['amount'], 'metadata': body['metadata']}
        if 'confirmation' in body:
            obj['confirmation'] = {'type': 'redirect', 'confirmation_url': 'https://yoomoney.ru/checkout/' + pid}
        self.objects[pid], self.by_key[key] = obj, pid
        if 'payment_method_id' in body:
            status, reason = self.renewal
            if status == 'succeeded':self.pay(pid, method={'id': body['payment_method_id'], 'saved': True})
            elif status == 'canceled':self.cancel(pid, reason)
        return copy.deepcopy(self.objects[pid])

    def pay(self, pid, method=None):
        self.objects[pid].update(status='succeeded', paid=True, payment_method=method or {
            'type': 'bank_card', 'id': 'pm-1', 'saved': True, 'title': 'Bank card *4444',
            'card': {'last4': '4444', 'card_type': 'Visa', 'first6': '555555'}})

    def cancel(self, pid, reason):
        self.objects[pid].update(status='canceled', cancellation_details={'party': 'yoo_money', 'reason': reason})

    def get_payment(self, pid):
        self.gets.append(pid)
        return copy.deepcopy(self.objects[pid])

    def create_refund(self, body, key):
        self.refunds.append((body, key))
        return {'id': 'rf-' + key[:8], 'status': 'succeeded', 'payment_id': body['payment_id'], 'amount': body['amount']}

    def get_refund(self, rid):
        return {'id': rid, 'status': 'succeeded'}


@unittest.skipUnless(fixtures.DSN, 'WEB_TEST_DATABASE_URL is not configured')
class PaymentsPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):cls.db.close()

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        with self.db:
            self.db.execute('DELETE FROM settings WHERE key LIKE ?', ('rl:%',))
            self.db.execute('UPDATE app_users SET terms_accepted_at=now()')
        self.kassa = FakeKassa()
        self.sent = []
        self.app.config.update(YOOKASSA_CLIENT_FACTORY=lambda: self.kassa, WEB_PUBLIC_URL='https://signalid.test',
                               TELEGRAM_SENDER=lambda method, payload: self.sent.append(payload))
        self.env = patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '', 'UNLIMITED_TELEGRAM_IDS': '', 'FEEDBACK_CHAT_ID': '777',
                                           'YOOKASSA_TRUST_PROXY': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tg_login(self):
        with self.client.session_transaction('/tg/feed') as state:state.update(user_id=self.u, csrf='tg-csrf')

    def subscription(self, uid=None):
        return self.db.execute('SELECT * FROM subscriptions WHERE user_id=?', (uid or self.u,)).fetchone()

    def checkout(self, plan='pro', period='month', email='boss@example.ru', consent='1', path='/app/billing/checkout', json_=True):
        headers = {'Accept': 'application/json'} if json_ else {}
        return self.client.post(path, data={'csrf': 'test-csrf', 'plan': plan, 'period': period, 'email': email,
                                            'consent': consent}, headers=headers)

    def webhook(self, pid, ip='185.71.76.10', event='payment.succeeded', status='succeeded'):
        body = {'type': 'notification', 'event': event, 'object': {'id': pid, 'status': status}}
        return self.client.post('/webhooks/yookassa', json=body, headers={'X-Real-IP': ip})

    def plan_changes(self):
        return self.db.execute("SELECT * FROM plan_changes WHERE user_id=? AND source='payment' ORDER BY id", (self.u,)).fetchall()

    def paid_pro(self):
        data = self.checkout().get_json()
        pid = self.kassa.by_key[data['payment_id']]
        self.kassa.pay(pid)
        self.assertEqual(self.webhook(pid).status_code, 200)
        return data, pid

    # --- checkout and activation ---------------------------------------------------------------
    def test_checkout_validates_and_creates_payment_with_receipt(self):
        self.assertEqual(self.checkout(email='bad').status_code, 400)
        self.assertIn('согласие', self.checkout(consent='').get_json()['error'])
        self.assertEqual(self.checkout(plan='free').status_code, 400)
        self.assertEqual(self.client.post('/app/billing/checkout', data={'plan': 'pro', 'period': 'month'}).status_code, 400)
        data = self.checkout('pro', 'year').get_json()
        self.assertEqual((data['status'], data['kind'], data['amount'], data['credit']), ('redirect', 'initial', '19104.00', '0.00'))
        self.assertTrue(data['confirmation_url'].startswith('https://yoomoney.ru/'))
        body, key = self.kassa.created[-1]
        self.assertEqual(key, data['payment_id'])
        self.assertTrue(body['save_payment_method'] and body['capture'])
        self.assertEqual(body['confirmation']['return_url'],
                         'https://signalid.test/app/billing/return?payment=' + data['payment_id'])
        self.assertEqual(body['receipt']['customer']['email'], 'boss@example.ru')
        self.assertEqual(body['receipt']['items'][0]['amount']['value'], '19104.00')
        self.assertEqual(self.db.execute('SELECT email FROM app_users WHERE id=?', (self.u,)).fetchone()['email'], 'boss@example.ru')
        self.assertIsNotNone(self.subscription()['autorenew_consent_at'])
        # A repeated click reuses the open payment.
        self.assertEqual(self.checkout('pro', 'year').get_json()['payment_id'], data['payment_id'])
        self.assertEqual(len(self.kassa.created), 1)
        # Without JSON the browser is redirected to the payment page.
        response = self.checkout('pro', 'year', json_=False)
        self.assertEqual((response.status_code, response.headers['Location']), (303, data['confirmation_url']))
        # Nothing is activated before the payment succeeds.
        self.assertNotEqual(self.subscription()['provider'], 'yookassa')

    def test_viewer_cannot_pay_and_disabled_without_credentials(self):
        with self.db:
            self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'viewer')", (self.ws, self.other))
        with self.client.session_transaction() as state:state.update(user_id=self.other, workspace_id=self.ws)
        self.assertEqual(self.checkout().status_code, 403)
        self.app.config['YOOKASSA_CLIENT_FACTORY'] = None
        with patch.dict(os.environ, {'YOOKASSA_SHOP_ID': '', 'YOOKASSA_SECRET_KEY': ''}):
            self.assertEqual(self.checkout().status_code, 404)
            self.assertEqual(self.webhook('yk-1').status_code, 404)

    def test_webhook_checks_ip_refetches_and_is_idempotent(self):
        data = self.checkout().get_json()
        pid = self.kassa.by_key[data['payment_id']]
        # Untrusted source: rejected before any API call.
        self.assertEqual(self.webhook(pid, ip='1.2.3.4').status_code, 403)
        self.assertEqual(self.kassa.gets, [])
        # The body claims success but the API says pending: nothing happens.
        self.assertEqual(self.webhook(pid).status_code, 200)
        self.assertEqual(self.kassa.gets, [pid])
        self.assertNotEqual(self.subscription()['provider'], 'yookassa')
        self.kassa.pay(pid)
        self.assertEqual(self.webhook(pid).status_code, 200)
        first = self.subscription()
        self.assertEqual((first['plan_code'], first['provider'], first['status'], first['autorenew']), ('pro', 'yookassa', 'active', True))
        self.assertEqual((first['payment_method_id'], first['payment_method_title']), ('pm-1', 'Bank card *4444'))
        # Repeated notification and the return page do not extend twice.
        self.assertEqual(self.webhook(pid).status_code, 200)
        page = self.client.get('/app/billing/return?payment=' + data['payment_id'], headers={'Accept': 'application/json'}).get_json()
        self.assertEqual((page['status'], page['plan'], page['autorenew']), ('succeeded', 'pro', True))
        self.assertEqual(self.subscription()['ends_at'], first['ends_at'])
        self.assertEqual(len(self.plan_changes()), 1)
        texts = [payload['text'] for payload in self.sent]
        self.assertEqual(sum('Оплата прошла' in text for text in texts), 1)
        self.assertTrue(any('Оплата ЮKassa' in text for text in texts))
        row = self.db.execute('SELECT * FROM payments WHERE id=?', (data['payment_id'],)).fetchone()
        self.assertEqual((row['status'], row['period_end']), ('succeeded', first['ends_at']))
        # Malformed notification and API outage.
        self.assertEqual(self.client.post('/webhooks/yookassa', json={'x': 1}, headers={'X-Real-IP': '185.71.76.10'}).status_code, 400)
        self.kassa.get_payment = lambda pid: (_ for _ in ()).throw(yookassa.YooKassaUnavailable('down'))
        self.assertEqual(self.webhook(pid).status_code, 503)

    def test_proxy_header_ignored_when_not_trusted(self):
        with patch.dict(os.environ, {'YOOKASSA_TRUST_PROXY': '0'}):
            self.assertEqual(self.webhook('yk-1').status_code, 403)

    def test_return_page_fallback_activates_and_canceled_shows_reason(self):
        data = self.checkout('start', 'month').get_json()
        pid = self.kassa.by_key[data['payment_id']]
        self.kassa.pay(pid)
        page = self.client.get('/app/billing/return?payment=' + data['payment_id'])
        self.assertEqual(page.status_code, 200)
        self.assertIn('подключён', page.text)
        self.assertEqual(self.subscription()['plan_code'], 'start')
        self.assertEqual(self.client.get('/app/billing/return?payment=00000000-0000-0000-0000-000000000000').status_code, 404)
        data = self.checkout('team', 'month').get_json()
        pid = self.kassa.by_key[data['payment_id']]
        self.kassa.cancel(pid, 'insufficient_funds')
        self.tg_login()
        page = self.client.get('/tg/billing/return?payment=' + data['payment_id'], headers={'Accept': 'application/json'}).get_json()
        self.assertEqual((page['status'], page['reason']), ('canceled', 'недостаточно средств'))
        self.assertEqual(self.subscription()['plan_code'], 'start')

    def test_trial_user_pays_and_period_starts_now(self):
        with self.db:
            self.db.execute("UPDATE subscriptions SET status='trial',plan_code='trial',ends_at=now()+interval '5 days' WHERE user_id=?", (self.u,))
        before = datetime.now(timezone.utc)
        self.paid_pro()
        row = self.subscription()
        self.assertEqual((row['status'], row['plan_code']), ('active', 'pro'))
        self.assertLess(abs((row['ends_at'] - billing.add_months(before, 1)).total_seconds()), 60)

    def test_upgrade_preview_and_charge_use_credit(self):
        self.paid_pro()
        with self.db:
            # Half of the paid month is used.
            self.db.execute('''UPDATE payments SET period_start=now()-interval '15 days',period_end=now()+interval '15 days',
                paid_at=now()-interval '15 days' WHERE user_id=?''', (self.u,))
            self.db.execute('''UPDATE subscriptions SET ends_at=(SELECT period_end FROM payments WHERE user_id=?) WHERE user_id=?''',
                            (self.u, self.u))
        data = self.client.get('/app/billing/preview?plan=team&period=month').get_json()
        self.assertEqual(data['kind'], 'upgrade')
        self.assertAlmostEqual(float(data['credit']), 995.0, delta=1)
        self.assertEqual(D(data['amount']) + D(data['credit']), D('4990.00'))
        self.assertEqual(self.client.get('/app/billing/preview?plan=vip').status_code, 400)
        created = self.checkout('team', 'month').get_json()
        self.assertEqual(created['kind'], 'upgrade')
        self.assertEqual(self.kassa.created[-1][0]['amount']['value'], created['amount'])
        self.kassa.pay(self.kassa.by_key[created['payment_id']])
        self.webhook(self.kassa.by_key[created['payment_id']])
        self.assertEqual(self.subscription()['plan_code'], 'team')

    def test_downgrade_is_scheduled_and_applied_at_renewal(self):
        self.paid_pro()
        data = self.checkout('start', 'month').get_json()
        self.assertEqual((data['status'], data['amount']), ('scheduled', '0.00'))
        row = self.subscription()
        self.assertEqual((row['plan_code'], row['scheduled_plan'], row['scheduled_period']), ('pro', 'start', 'month'))
        self.assertIn('уже подключён', self.checkout('pro', 'month').get_json()['error'])
        end = row['ends_at']
        results = payments.renew(self.db, self.kassa, lambda m, p: self.sent.append(p), now=end - timedelta(hours=23))
        self.assertEqual([r['event'] for r in results], ['activated'])
        body = self.kassa.created[-1][0]
        self.assertEqual((body['amount']['value'], body['payment_method_id']), ('990.00', 'pm-1'))
        self.assertNotIn('confirmation', body)
        row = self.subscription()
        self.assertEqual((row['plan_code'], row['scheduled_plan'], row['ends_at']), ('start', None, billing.add_months(end, 1)))

    def test_scheduled_downgrade_can_be_canceled(self):
        json_ = {'Accept': 'application/json'}
        response = self.client.post('/app/billing/scheduled/cancel', data={'csrf': 'test-csrf'}, headers=json_)
        self.assertEqual((response.status_code, response.get_json()['error']), (400, 'Смена тарифа не запланирована.'))
        self.paid_pro()
        self.checkout('start', 'month')
        self.assertIn('Отменить смену тарифа', self.client.get('/app/billing').text)
        data = self.client.post('/app/billing/scheduled/cancel', data={'csrf': 'test-csrf'}, headers=json_).get_json()
        self.assertTrue(data['ok'])
        row = self.subscription()
        self.assertEqual((row['plan_code'], row['scheduled_plan'], row['scheduled_period'], row['autorenew']),
                         ('pro', None, None, True))
        # The next renewal bills the current plan again.
        results = payments.renew(self.db, self.kassa, None, now=row['ends_at'] - timedelta(hours=23))
        self.assertEqual([r['event'] for r in results], ['activated'])
        self.assertEqual(self.kassa.created[-1][0]['amount']['value'], '1990.00')
        # Mini App route, form post with a redirect; a viewer may not change it.
        self.checkout('start', 'month')
        self.tg_login()
        response = self.client.post('/tg/billing/scheduled/cancel', data={'csrf': 'tg-csrf'})
        self.assertEqual((response.status_code, response.headers['Location']), (302, '/tg/billing'))
        self.assertIsNone(self.subscription()['scheduled_plan'])
        with self.db:
            self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'viewer')", (self.ws, self.other))
        with self.client.session_transaction() as state:state.update(user_id=self.other, workspace_id=self.ws, csrf='test-csrf')
        self.assertEqual(self.client.post('/app/billing/scheduled/cancel', data={'csrf': 'test-csrf'}, headers=json_).status_code, 403)

    def test_billing_pages_render_payment_ui(self):
        # Before any payment: plan cards with the checkout form, e-mail and consent.
        page = self.client.get('/app/billing').text
        for text in ('data-checkout', 'name="email"', 'name="consent"', '/terms#autorenew', 'Оплатить'):
            self.assertIn(text, page)
        self.assertNotIn('/app/billing/request', page)
        self.tg_login()
        page = self.client.get('/tg/billing').text
        self.assertIn('data-checkout', page)
        self.assertNotIn('/tg/billing/request', page)
        with self.client.session_transaction() as state:state.update(user_id=self.u, csrf='test-csrf')
        data, pid = self.paid_pro()
        page = self.client.get('/app/billing').text
        for text in ('Автопродление', 'Bank card *4444', 'Отключить автопродление', 'Перейти'):
            self.assertIn(text, page)
        # A failed renewal shows the grace period.
        with self.db:
            self.db.execute("UPDATE subscriptions SET status='past_due',grace_until=now()+interval '2 days' WHERE user_id=?", (self.u,))
        self.assertIn('Не удалось списать оплату', self.client.get('/app/billing').text)
        self.tg_login()
        page = self.client.get('/tg/billing').text
        self.assertIn('Не удалось списать оплату', page)
        self.assertIn('Bank card *4444', page)
        for path in ('/app/billing/return?payment=', '/tg/billing/return?payment='):
            page = self.client.get(path + data['payment_id'])
            self.assertEqual(page.status_code, 200, path)
            self.assertIn('data-payment-return', page.text)
        # Admin: payments table with a refund form.
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1'}):
            page = self.client.get('/admin').text
        self.assertIn('id="payments"', page)
        self.assertIn(f"/admin/payments/{data['payment_id']}/refund", page)
        self.assertIn(pid, page)

    # --- renewals ----------------------------------------------------------------------------------
    def test_renewal_success_never_charges_a_period_twice(self):
        self.paid_pro()
        end = self.subscription()['ends_at']
        sender = lambda m, p: self.sent.append(p)
        self.assertEqual(payments.renew(self.db, self.kassa, sender, now=end - timedelta(hours=25)), [])
        results = payments.renew(self.db, self.kassa, sender, now=end - timedelta(hours=20))
        self.assertEqual([r['event'] for r in results], ['activated'])
        self.assertIn('Подписка продлена', self.sent[-2]['text'])
        self.assertEqual(self.subscription()['ends_at'], billing.add_months(end, 1))
        created = len(self.kassa.created)
        payments.renew(self.db, self.kassa, sender, now=end - timedelta(hours=19))
        payments.renew(self.db, self.kassa, sender, now=end)
        self.assertEqual(len(self.kassa.created), created)
        # The unique index refuses a second live renewal of the same period.
        with self.assertRaises(Exception):
            with self.db:
                self.db.execute('''INSERT INTO payments(id,user_id,kind,plan_code,billing_period,amount,status,period_start)
                    VALUES(gen_random_uuid(),?,'renewal','pro','month',1990,'pending',?)''', (self.u, end))

    def test_renewal_failures_retry_then_expire_to_free(self):
        self.paid_pro()
        end = self.subscription()['ends_at']
        self.kassa.renewal = ('canceled', 'insufficient_funds')
        sender = lambda m, p: self.sent.append(p)
        self.assertEqual([r['event'] for r in payments.renew(self.db, self.kassa, sender, now=end - timedelta(hours=23))], ['retry'])
        row = self.subscription()
        self.assertEqual((row['status'], row['plan_code']), ('past_due', 'pro'))
        self.assertIn('Повторим попытку', self.sent[-1]['text'])
        self.assertIn('«Подписка»', self.sent[-1]['text'])
        # Not due again before the period end; the plan still works during grace.
        self.assertEqual(payments.renew(self.db, self.kassa, sender, now=end - timedelta(hours=1)), [])
        self.assertEqual(billing.owner_plan(self.db, self.u), 'pro')
        self.assertEqual([r['event'] for r in payments.renew(self.db, self.kassa, sender, now=end + timedelta(minutes=5))], ['retry'])
        self.assertEqual(payments.renew(self.db, self.kassa, sender, now=end + timedelta(days=1)), [])
        self.assertEqual(len(self.kassa.created), 3)
        with patch('leadgen.billing.datetime') as fake:
            fake.now.return_value = end + timedelta(days=1)
            fake.fromisoformat = datetime.fromisoformat
            self.assertEqual(billing.owner_plan(self.db, self.u), 'pro')
        results = payments.renew(self.db, self.kassa, sender, now=end + timedelta(days=2, minutes=5))
        self.assertEqual([r['event'] for r in results], ['expired'])
        row = self.subscription()
        self.assertEqual((row['plan_code'], row['status'], row['autorenew'], row['ends_at']), ('free', 'active', False, None))
        self.assertIn('бесплатный', self.sent[-1]['text'])
        self.assertEqual(payments.renew(self.db, self.kassa, sender, now=end + timedelta(days=5)), [])
        self.assertEqual(len(self.kassa.created), 4)
        keys = [key for _, key in self.kassa.created[1:]]
        self.assertEqual(len(set(keys)), 3)

    def test_revoked_permission_stops_autorenew_and_retry_after_unknown_outcome(self):
        self.paid_pro()
        end = self.subscription()['ends_at']
        self.kassa.unavailable = True
        self.assertEqual(payments.renew(self.db, self.kassa, None, now=end - timedelta(hours=23)), [])
        pending = self.db.execute("SELECT * FROM payments WHERE kind='renewal'").fetchall()
        self.assertEqual([row['status'] for row in pending], ['pending'])
        # The next run re-sends with the same Idempotence-Key (our row id).
        self.kassa.unavailable = False
        self.kassa.renewal = ('canceled', 'permission_revoked')
        results = payments.renew(self.db, self.kassa, None, now=end - timedelta(hours=22))
        self.assertEqual([r['event'] for r in results], ['lapsing'])
        self.assertEqual({key for _, key in self.kassa.created[1:]}, {str(pending[0]['id'])})
        row = self.subscription()
        self.assertEqual((row['autorenew'], row['payment_method_id'], row['plan_code'], row['status']), (False, None, 'pro', 'active'))
        self.assertEqual(payments.renew(self.db, self.kassa, None, now=end + timedelta(hours=1)), [])

    def test_autorenew_toggle(self):
        self.assertIn('после оплаты', self.client.post('/app/billing/autorenew', data={'csrf': 'test-csrf', 'enabled': '1', 'consent': '1'},
                                                       headers={'Accept': 'application/json'}).get_json()['error'])
        self.paid_pro()
        self.tg_login()
        response = self.client.post('/tg/billing/autorenew', data={'csrf': 'tg-csrf', 'enabled': '0'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.subscription()['autorenew'])
        self.assertEqual(payments.renew(self.db, self.kassa, None, now=self.subscription()['ends_at']), [])
        self.assertEqual(self.client.post('/app/billing/autorenew', data={'csrf': 'test-csrf', 'enabled': '1'},
                                          headers={'Accept': 'application/json'}).status_code, 400)
        data = self.client.post('/app/billing/autorenew', data={'csrf': 'test-csrf', 'enabled': '1', 'consent': '1'},
                                headers={'Accept': 'application/json'}).get_json()
        self.assertEqual(data['autorenew'], True)
        self.assertTrue(self.subscription()['autorenew'])

    # --- refunds -----------------------------------------------------------------------------------
    def test_admin_refund_full_and_partial(self):
        data, pid = self.paid_pro()
        self.assertEqual(self.client.post(f"/admin/payments/{data['payment_id']}/refund", data={'csrf': 'test-csrf'}).status_code, 403)
        with patch.dict(os.environ, {'WEB_ADMIN_TELEGRAM_IDS': '1'}):
            response = self.client.post(f"/admin/payments/{data['payment_id']}/refund", data={'csrf': 'test-csrf', 'amount': '500,50'})
            self.assertEqual(response.status_code, 302)
            with self.assertRaises(payments.PaymentError):payments.refund(self.db, self.kassa, data['payment_id'], '2000')
            row = payments.refund(self.db, self.kassa, data['payment_id'], None, self.u)
        self.assertEqual((row['amount'], row['status']), (D('1489.50'), 'succeeded'))
        bodies = [body for body, _ in self.kassa.refunds]
        self.assertEqual([b['amount']['value'] for b in bodies], ['500.50', '1489.50'])
        self.assertEqual(bodies[0]['payment_id'], pid)
        self.assertEqual(bodies[0]['receipt']['items'][0]['amount']['value'], '500.50')
        with self.assertRaises(payments.PaymentError):payments.refund(self.db, self.kassa, data['payment_id'])
        # A refund lowers the credit for a later upgrade.
        self.assertEqual(self.client.get('/app/billing/preview?plan=team&period=month').get_json()['credit'], '0.00')

    def test_cli_renew(self):
        self.paid_pro()
        with self.db:
            self.db.execute("UPDATE subscriptions SET ends_at=now()+interval '1 hour' WHERE user_id=?", (self.u,))
            self.db.execute("UPDATE payments SET period_end=now()+interval '1 hour'")
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = payments.main(['renew'], db_factory=lambda: fixtures.db_open(fixtures.DSN),
                                 client_factory=lambda: self.kassa, sender=lambda m, p: None)
        self.assertEqual(code, 0)
        self.assertIn('activated: 1', out.getvalue())
        with patch.dict(os.environ, {'YOOKASSA_SHOP_ID': '', 'YOOKASSA_SECRET_KEY': ''}), patch('sys.stdout', io.StringIO()):
            self.assertEqual(payments.main(['renew'], db_factory=lambda: None), 0)


if __name__ == '__main__':
    unittest.main()
