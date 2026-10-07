"""Mini App (/tg) and website login against the disposable local PostgreSQL; Telegram is faked."""
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_web_postgres as fixtures
from test_reply_postgres import Gateway
from leadgen.telegram_accounts import SessionCipher
from leadgen.tg_auth import sign_init_data
from leadgen.web_auth import approve_login

TOKEN = '123456:' + 'T' * 35


def init_data(telegram_id, token=TOKEN, start_param=None, auth_date=None, **user):
    fields = {'query_id': 'AAHq', 'auth_date': str(auth_date or int(time.time())),
              'user': json.dumps({'id': telegram_id, 'first_name': 'Тест', 'username': 'tester'} | user, ensure_ascii=False)}
    if start_param:
        fields['start_param'] = start_param
    return sign_init_data(fields, token)


@unittest.skipUnless(fixtures.DSN, 'WEB_TEST_DATABASE_URL is not configured')
class MiniAppPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.WebPostgres.setUpClass.__func__(cls)
        cls.app.config['TELEGRAM_BOT_TOKEN'] = TOKEN

    @classmethod
    def tearDownClass(cls):
        fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        with self.db:
            self.db.execute('DELETE FROM settings WHERE key LIKE ? OR key LIKE ?', ('rl:%', 'web_handoff:%'))
            self.db.execute('UPDATE app_users SET terms_accepted_at=now()')
        self.tg = self.app.test_client()

    def tg_login(self, client=None, uid=None, **extra):
        client = client or self.tg
        with client.session_transaction('/tg/feed') as state:
            state.update(user_id=uid or self.u, csrf='tg-csrf', **extra)
        return client

    def auth(self, client, raw, **form):
        return client.post('/tg/auth', data={'init_data': raw} | form, headers={'Origin': 'http://localhost'})

    # --- /tg/auth ---------------------------------------------------------------------------------
    def test_auth_registers_new_user_without_terms_then_onboards(self):
        r = self.auth(self.tg, init_data(777001, first_name='Новый', last_name='Клиент'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json['ok'])
        self.assertTrue(r.json['next'].startswith('/tg/start'))
        user = self.db.execute('SELECT * FROM app_users WHERE telegram_user_id=777001').fetchone()
        self.assertEqual((user['telegram_chat_id'], user['display_name']), (777001, 'Новый Клиент'))
        self.assertIsNone(user['terms_accepted_at'])
        prefs = self.db.execute('SELECT topics,monitoring_active FROM user_preferences WHERE user_id=?', (user['id'],)).fetchone()
        self.assertEqual((prefs['topics'], prefs['monitoring_active']), ([], False))
        self.assertTrue(self.db.execute('SELECT 1 FROM subscriptions WHERE user_id=?', (user['id'],)).fetchone())
        # Only the Mini App cookie is set, never the cabinet cookie.
        self.assertIsNotNone(self.tg.get_cookie('signalid_tg', path='/tg'))
        self.assertIsNone(self.tg.get_cookie('signalid_session'))
        self.assertEqual(self.tg.get('/app').status_code, 302)
        # Consent is step 1: every Mini App page leads to /tg/start until it is given.
        for path in ('/tg/feed', '/tg/settings', '/tg/billing', '/tg/start?step=3'):
            response = self.tg.get(path)
            if path.startswith('/tg/start'):
                self.assertIn('name="consent"', response.text)
            else:
                self.assertEqual(response.status_code, 302, path)
                self.assertTrue(response.location.startswith('/tg/start'), response.location)
        with self.tg.session_transaction('/tg/feed') as state:
            csrf = state['csrf']
        self.assertEqual(self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'services', 'topics': ['Сайты']}).status_code, 302)
        self.assertEqual(self.db.execute('SELECT topics FROM user_preferences WHERE user_id=?', (user['id'],)).fetchone()['topics'], [])
        r = self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'consent'})
        self.assertIsNone(self.db.execute('SELECT terms_accepted_at FROM app_users WHERE id=?', (user['id'],)).fetchone()['terms_accepted_at'])
        r = self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'consent', 'consent': '1'})
        self.assertIn('step=2', r.location)
        self.assertIsNotNone(self.db.execute('SELECT terms_accepted_at FROM app_users WHERE id=?', (user['id'],)).fetchone()['terms_accepted_at'])
        page = self.tg.get(r.location)
        self.assertIn('data-service-picker', page.text)
        self.assertIn('service_picker.js', page.text)
        self.assertEqual(self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'services', 'topics': ['Нет такой']}).status_code, 400)
        r = self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'services', 'topics': ['Сайты', 'Боты']})
        self.assertIn('step=3', r.location)
        self.assertEqual(self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'budget', 'min_budget': '7777'}).status_code, 400)
        r = self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'budget', 'min_budget': '30000', 'no_budget': '1'})
        self.assertIn('step=4', r.location)
        self.assertIn('Запустить', self.tg.get(r.location).text)
        r = self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'launch'})
        self.assertEqual(r.location, '/tg/feed')
        prefs = self.db.execute('SELECT * FROM user_preferences WHERE user_id=?', (user['id'],)).fetchone()
        self.assertEqual((prefs['topics'], prefs['min_budget'], prefs['show_without_budget'], prefs['monitoring_active']),
                         (['Сайты', 'Боты'], 30000, True, True))
        self.assertEqual(self.tg.get('/tg/feed').status_code, 200)

    def test_auth_existing_user_follows_start_param_and_next(self):
        r = self.auth(self.tg, init_data(1, start_param=f'lead_{self.ids[0]}'))
        self.assertEqual(r.json, {'ok': True, 'next': f'/tg/leads/{self.ids[0]}'})
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM app_users').fetchone()['n'], 2)
        page = self.tg.get(r.json['next'])
        self.assertEqual(page.status_code, 200)
        self.assertIn('Нужен сайт 0', page.text)
        self.assertIn('data-tg-user="1"', page.text)
        self.assertEqual(self.auth(self.app.test_client(), init_data(1, start_param='billing')).json['next'], '/tg/billing')
        self.assertEqual(self.auth(self.app.test_client(), init_data(1), next='/tg/settings').json['next'], '/tg/settings')
        self.assertEqual(self.auth(self.app.test_client(), init_data(1), next='https://evil.example').json['next'], '/tg/feed')
        # The boot page sends a logged-in user straight on.
        self.assertEqual(self.tg.get('/tg?next=/tg/billing').location, '/tg/billing')

    def test_existing_user_without_terms_skips_configured_steps(self):
        with self.db:
            self.db.execute('UPDATE app_users SET terms_accepted_at=NULL WHERE id=?', (self.u,))
        r = self.auth(self.tg, init_data(1), next=f'/tg/leads/{self.ids[0]}')
        self.assertTrue(r.json['next'].startswith('/tg/start?step=1&next='))
        with self.tg.session_transaction('/tg/feed') as state:
            csrf = state['csrf']
        r = self.tg.post('/tg/start', data={'csrf': csrf, 'action': 'consent', 'consent': '1', 'next': f'/tg/leads/{self.ids[0]}'})
        self.assertEqual(r.location, f'/tg/leads/{self.ids[0]}')

    def test_invalid_init_data_is_rejected_and_logs_nobody_in(self):
        good = init_data(1)
        tampered = good.replace('tester', 'attacker')
        for raw in (tampered, init_data(1, token='999:' + 'X' * 35), init_data(1, auth_date=int(time.time()) - 90000), ''):
            client = self.app.test_client()
            r = self.auth(client, raw)
            self.assertEqual(r.status_code, 401)
            self.assertFalse(r.json['ok'])
            self.assertEqual(client.get('/tg/feed').status_code, 302)
        self.assertFalse(self.db.execute("SELECT 1 FROM app_users WHERE username='attacker'").fetchone())
        with self.db:
            self.db.execute("UPDATE app_users SET status='blocked' WHERE id=?", (self.u,))
        self.assertEqual(self.auth(self.app.test_client(), good).status_code, 403)

    def test_auth_rate_limited_per_ip(self):
        client = self.app.test_client()
        codes = [self.auth(client, 'bad').status_code for _ in range(61)]
        self.assertEqual(codes[:60], [401] * 60)
        self.assertEqual(codes[60], 429)

    def test_auth_unconfigured_token(self):
        self.app.config['TELEGRAM_BOT_TOKEN'] = ''
        try:
            self.assertEqual(self.auth(self.app.test_client(), init_data(1)).status_code, 503)
        finally:
            self.app.config['TELEGRAM_BOT_TOKEN'] = TOKEN

    # --- gating ----------------------------------------------------------------------------------
    def test_gating_session_terms_and_subscription(self):
        r = self.app.test_client().get(f'/tg/leads/{self.ids[0]}')
        self.assertEqual(r.location, f'/tg?next=/tg/leads/{self.ids[0]}')
        self.tg_login()
        self.assertEqual(self.tg.get('/tg/feed').status_code, 200)
        with self.db:
            self.db.execute('UPDATE app_users SET terms_accepted_at=NULL WHERE id=?', (self.u,))
        r = self.tg.get(f'/tg/leads/{self.ids[0]}')
        self.assertEqual(r.location, f'/tg/start?next=/tg/leads/{self.ids[0]}')
        with self.db:
            self.db.execute('UPDATE app_users SET terms_accepted_at=now() WHERE id=?', (self.u,))
            self.db.execute("UPDATE subscriptions SET ends_at=now()-interval '1 minute' WHERE user_id=?", (self.u,))
        for path in ('/tg/feed', f'/tg/leads/{self.ids[0]}', '/tg/settings'):
            self.assertEqual(self.tg.get(path).location, '/tg/billing', path)
        r = self.tg.post(f'/tg/leads/{self.ids[0]}', data={'csrf': 'tg-csrf', 'action': 'status', 'status': 'won'})
        self.assertEqual(r.location, '/tg/billing')
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE lead_id=?', (self.ids[0],)).fetchone()['pipeline_status'], 'saved')
        page = self.tg.get('/tg/billing')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Доступ приостановлен', page.text)
        self.assertIn('data-handoff="/app/billing"', page.text)
        self.assertEqual(self.tg.post('/tg/handoff', data={'csrf': 'tg-csrf', 'next': '/app/billing'}).status_code, 200)

    def test_tg_and_cabinet_sessions_are_separate(self):
        self.tg_login()
        self.assertEqual(self.tg.get('/app').status_code, 302)
        self.assertEqual(self.client.get('/tg/feed').status_code, 302)
        self.assertEqual(self.client.get('/app').status_code, 200)

    def test_csrf_required_for_tg_forms(self):
        self.tg_login()
        r = self.tg.post(f'/tg/leads/{self.ids[0]}', data={'action': 'status', 'status': 'won'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('tg.css', r.text)

    # --- feed ------------------------------------------------------------------------------------
    def test_feed_is_scoped_filtered_and_keyset_paginated(self):
        self.tg_login()
        page = self.tg.get('/tg/feed')
        self.assertIn('Нужен сайт 0', page.text)
        self.assertNotIn('Нужен сайт 1', page.text)
        self.assertNotIn('<script>alert(1)</script>', page.text)
        self.assertIn('data-root="1"', page.text)
        with self.db:
            for i in range(25):
                payload = dict(source='Telegram: test', url=f'https://t.me/test/{100 + i}', title=f'Пачка {i:02d}',
                               text='Нужен лендинг', published=datetime.now(timezone.utc).isoformat())
                lid = self.db.execute('INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?) RETURNING id',
                                      (payload['url'], f'p{i}', json.dumps(payload), 'ready', 'test', payload['published'])).fetchone()['id']
                self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'sent')", (self.u, lid))
            self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'filtered')", (self.other, self.ids[0]))
        first = self.tg.get('/tg/feed').text
        self.assertEqual(first.count('class="lead-item"'), 20)
        self.assertIn('Пачка 24', first)
        self.assertNotIn('Пачка 04', first)
        before = first.split('before=')[1].split('"')[0]
        second = self.tg.get('/tg/feed?before=' + before).text
        self.assertEqual(second.count('class="lead-item"'), 6)
        self.assertIn('Пачка 04', second)
        self.assertIn('Нужен сайт 0', second)
        self.assertNotIn('before=', second)
        with self.db:
            self.db.execute("UPDATE user_leads SET pipeline_status='working' WHERE user_id=? AND lead_id=?", (self.u, self.ids[0]))
            self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')", (self.u, self.ids[0]))
            self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')", (self.other, self.ids[1]))
        for kind in ('work', 'fit'):
            text = self.tg.get('/tg/feed?f=' + kind).text
            self.assertEqual(text.count('class="lead-item"'), 1, kind)
            self.assertIn('Нужен сайт 0', text)
        self.assertNotIn('Нужен сайт 0', self.tg.get('/tg/feed?f=new').text)
        for bad in ('?f=all', '?before=abc', '?before=' + '9' * 30):
            self.assertEqual(self.tg.get('/tg/feed' + bad).status_code, 400, bad)

    # --- lead card -------------------------------------------------------------------------------
    def test_lead_status_feedback_and_reminder_side_effects(self):
        self.tg_login()
        lid = self.ids[0]
        events = []
        with patch('leadgen.integrations.enqueue_lead_event', side_effect=lambda db, owner, lead, kind: events.append((owner, lead, kind))):
            r = self.tg.post(f'/tg/leads/{lid}', data={'csrf': 'tg-csrf', 'action': 'status', 'status': 'working'})
            self.assertEqual(r.location, f'/tg/leads/{lid}')
            r = self.tg.post(f'/tg/leads/{lid}', data={'csrf': 'tg-csrf', 'action': 'feedback', 'feedback': 'ad'})
            self.assertEqual(r.status_code, 302)
        self.assertEqual(events, [(self.u, lid, 'lead.updated')] * 2)
        row = self.db.execute('SELECT pipeline_status FROM user_leads WHERE user_id=? AND lead_id=?', (self.u, lid)).fetchone()
        self.assertEqual(row['pipeline_status'], 'working')
        self.assertEqual(self.db.execute('SELECT label FROM lead_feedback WHERE user_id=? AND lead_id=?', (self.u, lid)).fetchone()['label'], 'ad')
        activity = [(a['kind'], a['detail']) for a in self.db.execute(
            'SELECT kind,detail FROM lead_activity WHERE user_id=? AND lead_id=? ORDER BY id', (self.u, lid))]
        self.assertEqual(activity, [('status', 'В работе'), ('feedback', 'Реклама')])
        page = self.tg.get(f'/tg/leads/{lid}')
        self.assertIn('Статус: В работе', page.text)
        self.assertIn('data-haptic="success"', page.text)
        self.assertIn('aria-pressed="true">В работе', page.text)
        self.tg.post(f'/tg/leads/{lid}', data={'csrf': 'tg-csrf', 'action': 'feedback', 'feedback': ''})
        self.assertFalse(self.db.execute('SELECT 1 FROM lead_feedback WHERE user_id=? AND lead_id=?', (self.u, lid)).fetchone())
        self.tg.post(f'/tg/leads/{lid}', data={'csrf': 'tg-csrf', 'action': 'remind'})
        reminder = self.db.execute('SELECT status,due_at FROM lead_reminders WHERE user_id=? AND lead_id=?', (self.u, lid)).fetchone()
        self.assertEqual(reminder['status'], 'pending')
        self.assertAlmostEqual((reminder['due_at'] - datetime.now(timezone.utc)).total_seconds(), 3600, delta=120)
        self.assertIn('Напомним', self.tg.get(f'/tg/leads/{lid}').text)
        for data in ({'action': 'status', 'status': 'meeting'}, {'action': 'status', 'status': 'nope'},
                     {'action': 'feedback', 'feedback': 'nope'}, {'action': 'delete'}):
            self.assertEqual(self.tg.post(f'/tg/leads/{lid}', data={'csrf': 'tg-csrf'} | data).status_code, 400, data)

    def test_lead_card_is_owner_scoped_and_role_checked(self):
        self.tg_login()
        self.assertEqual(self.tg.get(f'/tg/leads/{self.ids[1]}').status_code, 404)
        self.assertEqual(self.tg.post(f'/tg/leads/{self.ids[1]}', data={'csrf': 'tg-csrf', 'action': 'status', 'status': 'won'}).status_code, 404)
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE lead_id=?', (self.ids[1],)).fetchone()['pipeline_status'], 'saved')
        with self.db:
            self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'viewer')", (self.ws, self.other))
        viewer = self.tg_login(self.app.test_client(), self.other, workspace_id=self.ws)
        page = viewer.get(f'/tg/leads/{self.ids[0]}')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('name="status"', page.text)
        self.assertEqual(viewer.post(f'/tg/leads/{self.ids[0]}', data={'csrf': 'tg-csrf', 'action': 'status', 'status': 'won'}).status_code, 403)
        self.assertEqual(viewer.get(f'/tg/leads/{self.ids[0]}/reply').status_code, 403)
        self.assertEqual(viewer.post('/tg/settings', data={'csrf': 'tg-csrf', 'min_budget': '1'}).status_code, 403)

    # --- replies ---------------------------------------------------------------------------------
    def connect_telegram(self):
        cipher = SessionCipher(self.app.config['TELEGRAM_CIPHER_KEY'])
        gateway = Gateway()
        self.app.config['TELEGRAM_REPLY_GATEWAY_FACTORY'] = lambda: gateway
        with self.db:
            self.db.execute('''INSERT INTO telegram_connections(user_id,telegram_user_id,session_cipher,status,display_name)
                VALUES(?,9001,?,'active','Мой аккаунт')''', (self.u, cipher.encrypt({'session': '9001'})))
            payload = json.loads(self.db.execute('SELECT payload FROM leads WHERE id=?', (self.ids[0],)).fetchone()['payload'])
            payload['url'] = 'https://t.me/business_chat/101'
            self.db.execute('UPDATE leads SET payload=?,url=? WHERE id=?', (json.dumps(payload), payload['url'], self.ids[0]))
        return gateway

    def test_reply_without_personal_telegram_hands_off_to_website(self):
        self.tg_login()
        page = self.tg.get(f'/tg/leads/{self.ids[0]}/reply')
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-handoff="/app/telegram"', page.text)
        self.assertNotIn('data-main-button', page.text)
        r = self.tg.post(f'/tg/leads/{self.ids[0]}/reply', data={'csrf': 'tg-csrf', 'body': 'Привет', 'mode': 'dm'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.db.execute('SELECT 1 FROM telegram_replies').fetchone())

    def test_reply_prepare_confirm_and_no_resend(self):
        gateway = self.connect_telegram()
        self.tg_login()
        lid = self.ids[0]
        self.assertIn(f'/tg/leads/{lid}/reply', self.tg.get(f'/tg/leads/{lid}').text)
        page = self.tg.get(f'/tg/leads/{lid}/reply')
        self.assertIn('data-main-button="Проверить получателя"', page.text)
        for name in ('Короткий', 'Экспертный', 'Дружелюбный'):
            self.assertIn(name, page.text)
        chosen = self.tg.get(f'/tg/leads/{lid}/reply', query_string={'v': 'tpl:Экспертный', 'mode': 'group'}).text
        self.assertIn('required>Здравствуйте! Вижу задачу', chosen)
        self.assertIn('value="group" checked', chosen)
        with patch.dict('os.environ', {'GROQ_API_KEY': 'k', 'GROQ_MODEL': 'm'}):
            self.client.post(f'/app/leads/{lid}/ai-offers', data={'csrf': 'test-csrf', 'project_id': ''})
        page = self.tg.get(f'/tg/leads/{lid}/reply')
        self.assertIn('AI · Короткий', page.text)
        self.assertIn('Готов обсудить вашу задачу', page.text)
        r = self.tg.post(f'/tg/leads/{lid}/reply', data={'csrf': 'tg-csrf', 'mode': 'group', 'body': 'Текст <b>отклика</b>'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.location.startswith('/tg/replies/'))
        confirm = self.tg.get(r.location)
        self.assertIn('data-main-button="Отправить"', confirm.text)
        self.assertIn('&lt;b&gt;', confirm.text)
        self.assertIn('Группа', confirm.text)
        self.assertEqual(gateway.sent, [])
        self.tg.post(r.location, data={'csrf': 'tg-csrf', 'action': 'send'})
        self.tg.post(r.location, data={'csrf': 'tg-csrf', 'action': 'send'})
        self.assertEqual(len(gateway.sent), 1)
        self.assertEqual(gateway.sent[0][2], 'group')
        done = self.tg.get(r.location)
        self.assertIn('Отправлено', done.text)
        self.assertNotIn('data-main-button', done.text)
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE user_id=? AND lead_id=?',
                                         (self.u, lid)).fetchone()['pipeline_status'], 'contacted')
        r = self.tg.post(f'/tg/leads/{lid}/reply', data={'csrf': 'tg-csrf', 'mode': 'dm', 'body': ''})
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-haptic="error"', r.text)
        r = self.tg.post(f'/tg/leads/{lid}/reply', data={'csrf': 'tg-csrf', 'mode': 'dm', 'body': 'Отменю'})
        self.tg.post(r.location, data={'csrf': 'tg-csrf', 'action': 'cancel'})
        self.assertIn('Отменено', self.tg.get(r.location).text)
        self.assertEqual(len(gateway.sent), 1)
        self.assertEqual(self.tg.post(r.location, data={'csrf': 'tg-csrf', 'action': 'other'}).status_code, 400)
        other = self.tg_login(self.app.test_client(), self.other)
        self.assertEqual(other.get(r.location).status_code, 404)
        self.assertEqual(other.post(f'/tg/leads/{lid}/reply', data={'csrf': 'tg-csrf', 'body': 'x', 'mode': 'dm'}).status_code, 404)

    # --- settings and billing --------------------------------------------------------------------
    def test_settings_persist(self):
        self.tg_login()
        page = self.tg.get('/tg/settings')
        self.assertIn('data-service-picker', page.text)
        self.assertIn('data-main-button="Сохранить"', page.text)
        r = self.tg.post('/tg/settings', data={'csrf': 'tg-csrf', 'min_budget': '15000', 'topics': ['Сайты'], 'possible': '1'})
        self.assertEqual(r.location, '/tg/settings')
        prefs = self.db.execute('SELECT * FROM user_preferences WHERE user_id=?', (self.u,)).fetchone()
        self.assertEqual((prefs['min_budget'], prefs['topics'], prefs['monitoring_active'], prefs['show_possible_needs'],
                          prefs['show_without_budget']), (15000, ['Сайты'], False, True, False))
        for data in ({'min_budget': 'x'}, {'min_budget': '1', 'topics': ['Нет такой']}, {'min_budget': '999999999'}):
            self.assertEqual(self.tg.post('/tg/settings', data={'csrf': 'tg-csrf'} | data).status_code, 400, data)

    # --- handoff to the website ------------------------------------------------------------------
    def test_handoff_is_single_use_and_next_is_safe(self):
        self.tg_login()
        r = self.tg.post('/tg/handoff', data={'csrf': 'tg-csrf', 'next': '/app/billing'})
        self.assertTrue(r.json['ok'])
        url = urlsplit(r.json['url'])
        self.assertEqual(url.path, '/login/handoff')
        self.assertEqual(parse_qs(url.query)['next'], ['/app/billing'])
        browser = self.app.test_client()
        landing = browser.get(url.path + '?' + url.query)
        self.assertEqual(landing.location, '/app/billing')
        self.assertEqual(browser.get('/app').status_code, 200)
        self.assertIsNotNone(browser.get_cookie('signalid_session'))
        again = self.app.test_client().get(url.path + '?' + url.query)
        self.assertEqual(again.location, '/login')
        for target in ('https://evil.example/app', '//evil.example', '/tg/feed', '/admin', '/app/../admin'):
            r = self.tg.post('/tg/handoff', data={'csrf': 'tg-csrf', 'next': target})
            link = urlsplit(r.json['url'])
            self.assertEqual(parse_qs(link.query)['next'], ['/app'], target)
            token = parse_qs(link.query)['t'][0]
            self.assertEqual(self.app.test_client().get('/login/handoff', query_string={'t': token, 'next': target}).location, '/app', target)
        self.assertEqual(self.tg.post('/tg/handoff', data={'next': '/app'}).status_code, 400)
        r = self.tg.post('/tg/handoff', data={'csrf': 'tg-csrf'})
        token = parse_qs(urlsplit(r.json['url']).query)['t'][0]
        with self.db:
            self.db.execute('UPDATE app_users SET session_version=session_version+1 WHERE id=?', (self.u,))
        self.assertEqual(self.app.test_client().get('/login/handoff', query_string={'t': token}).location, '/login')

    def test_handoff_expires_and_uses_public_url(self):
        self.tg_login()
        self.app.config['WEB_PUBLIC_URL'] = 'https://signalid.example'
        try:
            r = self.tg.post('/tg/handoff', data={'csrf': 'tg-csrf'})
        finally:
            self.app.config['WEB_PUBLIC_URL'] = ''
        self.assertTrue(r.json['url'].startswith('https://signalid.example/login/handoff?t='))
        token = parse_qs(urlsplit(r.json['url']).query)['t'][0]
        with patch('leadgen.web_auth.time.time', return_value=time.time() + 61):
            self.assertEqual(self.app.test_client().get('/login/handoff', query_string={'t': token}).location, '/login')

    def test_boot_fallback_handoff_requires_terms(self):
        r = self.auth(self.app.test_client(), init_data(1), handoff='1')
        self.assertTrue(r.json['handoff'].startswith('http://localhost/login/handoff?t='))
        r = self.auth(self.app.test_client(), init_data(777002), handoff='1')
        self.assertNotIn('handoff', r.json)

    # --- website login: auto-complete, consent and session_version --------------------------------
    def test_login_status_auto_complete_records_consent_and_version(self):
        with self.db:
            self.db.execute('UPDATE app_users SET terms_accepted_at=NULL,session_version=4 WHERE id=?', (self.u,))
        c = self.app.test_client()
        c.get('/login')
        with c.session_transaction() as s:
            csrf = s['csrf']
        page = c.post('/login', data={'csrf': csrf, 'action': 'begin', 'consent': '1'})
        self.assertIn('login.js', page.text)
        self.assertIn('data-login-finish', page.text)
        self.assertEqual(c.get('/login/status').json, {'status': 'pending'})
        with c.session_transaction() as s:
            token, code = s['login_token'], s['login_code']
        approve_login(self.db, token, self.u, code)
        self.assertEqual(c.get('/login/status').json, {'status': 'approved'})
        self.assertEqual(c.post('/login', data={'csrf': csrf, 'action': 'finish'}).status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual((s['user_id'], s['sv']), (self.u, 4))
        self.assertEqual(c.get('/app').status_code, 200)
        self.assertEqual(c.get('/login/status').json, {'status': 'signed_in'})
        self.assertIsNotNone(self.db.execute('SELECT terms_accepted_at FROM app_users WHERE id=?', (self.u,)).fetchone()['terms_accepted_at'])
        stale = self.app.test_client()
        stale.get('/login')
        with stale.session_transaction() as s:
            s['login_token'] = 'x' * 43
        self.assertEqual(stale.get('/login/status').json, {'status': 'expired'})

    def test_logout_everywhere_revokes_cabinet_and_mini_app_sessions(self):
        self.tg_login()
        second = self.app.test_client()
        with second.session_transaction() as s:
            s.update(user_id=self.u, csrf='test-csrf')
        self.assertEqual(self.client.get('/app/security').status_code, 200)
        self.assertIn('Выйти на всех устройствах', self.client.get('/app/security').text)
        r = self.client.post('/app/security/logout-all', data={'csrf': 'test-csrf'})
        self.assertEqual(r.location, '/login')
        self.assertEqual(self.db.execute('SELECT session_version FROM app_users WHERE id=?', (self.u,)).fetchone()['session_version'], 1)
        for client in (self.client, second):
            self.assertEqual(client.get('/app').location, '/login')
        self.assertTrue(self.tg.get('/tg/feed').location.startswith('/tg?next='))
        # A fresh sign-in carries the new version and works again.
        self.assertEqual(self.auth(self.tg, init_data(1)).status_code, 200)
        self.assertEqual(self.tg.get('/tg/feed').status_code, 200)
        self.assertEqual(self.client.post('/app/security/logout-all', data={'csrf': 'test-csrf'}).status_code, 400)

    def test_security_page_open_with_expired_subscription(self):
        with self.db:
            self.db.execute("UPDATE subscriptions SET ends_at=now()-interval '1 minute' WHERE user_id=?", (self.u,))
        self.assertEqual(self.client.get('/app/security').status_code, 200)


if __name__ == '__main__':
    unittest.main()
