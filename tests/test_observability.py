"""Error tracking: no-op without SENTRY_DSN and privacy scrubbing of events and breadcrumbs."""
import os
import unittest
from unittest.mock import patch

from leadgen import observability as obs

BOT_TOKEN = '1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ'
GROQ_KEY = 'gsk_9fJk2LmN4pQr6StUv8WxYz0AbCdEfGh'


class ScrubTextTests(unittest.TestCase):
    def test_phones_redacted(self):
        for phone in ('+7 999 123-45-67', '+79991234567', '8 (999) 123-45-67', '89991234567', '999 123-45-67', '+44 20 7946 0958'):
            with self.subTest(phone=phone):
                out = obs.scrub_text('звоните ' + phone + ' вечером')
                self.assertNotIn(phone, out)
                self.assertIn('[phone]', out)

    def test_emails_redacted(self):
        out = obs.scrub_text('Пишите owner.name+tag@mail.example.ru сегодня')
        self.assertNotIn('owner.name', out)
        self.assertIn('[email]', out)

    def test_tokens_and_keys_redacted(self):
        text = (f'GET https://api.telegram.org/bot{BOT_TOKEN}/getUpdates failed; token {BOT_TOKEN}; '
                f'key={GROQ_KEY}; Authorization: Bearer abcdefghijklmnop123; api_key="secretvalue123"')
        out = obs.scrub_text(text)
        for secret in (BOT_TOKEN, GROQ_KEY, 'abcdefghijklmnop123', 'secretvalue123'):
            self.assertNotIn(secret, out)

    def test_database_url_credentials_redacted(self):
        out = obs.scrub_text('connect failed: postgresql://postgres.ref:S3cr3tPass@pooler.example.com:5432/postgres')
        self.assertNotIn('S3cr3tPass', out)
        self.assertNotIn('postgres.ref', out)
        self.assertIn('pooler.example.com', out)

    def test_ordinary_text_kept(self):
        text = 'KeyError in score_pending: lead 4521, status 500, 2026-10-11'
        self.assertEqual(obs.scrub_text(text), text)


class BeforeSendTests(unittest.TestCase):
    def event(self):
        return {
            'message': 'Ошибка для +7 999 123-45-67',
            'logentry': {'message': 'user %s', 'params': ['a@b.ru']},
            'user': {'id': 42, 'username': 'ivan', 'email': 'ivan@example.com', 'ip_address': '1.2.3.4'},
            'request': {'method': 'POST', 'url': 'https://signalid.ru/app/leads?q=ivan@example.com',
                        'query_string': 'q=ivan@example.com', 'data': {'note': 'секрет'},
                        'cookies': {'signalid_session': 'abc'}, 'env': {'REMOTE_ADDR': '1.2.3.4'},
                        'headers': {'Cookie': 'signalid_session=abc', 'Authorization': 'Bearer tok123456789',
                                    'User-Agent': 'Mozilla/5.0', 'X-Forwarded-For': '1.2.3.4'}},
            'exception': {'values': [{'type': 'RuntimeError', 'value': f'bot{BOT_TOKEN} failed',
                                      'stacktrace': {'frames': [{'function': 'f', 'vars': {'password': 'x'}}]}}]},
            'extra': {'DATABASE_URL': 'postgresql://u:p@h/db', 'detail': f'groq {GROQ_KEY}'},
            'breadcrumbs': {'values': [{'message': 'mail me at x@y.com', 'data': {'phone': '+79991234567'}}]},
        }

    def test_request_and_user_stripped(self):
        event = obs.before_send(self.event(), {})
        self.assertEqual(event['user'], {'id': '42'})
        request = event['request']
        self.assertEqual(request['url'], 'https://signalid.ru/app/leads')
        self.assertEqual(set(request), {'method', 'url', 'headers'})
        self.assertEqual(request['headers'], {'User-Agent': 'Mozilla/5.0'})

    def test_no_secrets_or_pii_anywhere(self):
        dumped = repr(obs.before_send(self.event(), {}))
        for leaked in ('999 123-45-67', '79991234567', 'a@b.ru', 'x@y.com', 'ivan', BOT_TOKEN, GROQ_KEY,
                       'u:p@', 'секрет', 'signalid_session=abc', 'tok123456789', "'vars'", '1.2.3.4'):
            self.assertNotIn(leaked, dumped)

    def test_non_numeric_user_dropped(self):
        event = obs.before_send({'user': {'id': 'ivan', 'username': 'ivan'}}, {})
        self.assertNotIn('user', event)

    def test_breadcrumb_scrubbed(self):
        crumb = obs.before_breadcrumb({'category': 'httplib', 'message': 'to a@b.ru',
                                       'data': {'url': f'https://api.telegram.org/bot{BOT_TOKEN}/send?chat=1',
                                                'http.query': 'chat=1'}}, {})
        self.assertNotIn(BOT_TOKEN, repr(crumb))
        self.assertNotIn('a@b.ru', repr(crumb))
        self.assertEqual(crumb['data']['http.query'], '')
        self.assertNotIn('?', crumb['data']['url'])


class NoopTests(unittest.TestCase):
    def tearDown(self):
        obs._enabled = False

    def test_noop_without_dsn(self):
        with patch.dict(os.environ, {'SENTRY_DSN': ''}), patch('sentry_sdk.init') as init:
            self.assertFalse(obs.init_sentry('web'))
            init.assert_not_called()
        self.assertFalse(obs.enabled())
        self.assertIsNone(obs.capture_exception(RuntimeError('x')))
        obs.set_user(1)
        obs.flush()

    def test_init_with_dsn_uses_private_settings(self):
        env = {'SENTRY_DSN': 'https://public@glitchtip.example.invalid/1', 'SENTRY_TRACES_SAMPLE_RATE': 'bad',
               'SENTRY_ENVIRONMENT': '', 'SENTRY_RELEASE': 'abc123'}
        with patch.dict(os.environ, env), patch('sentry_sdk.init') as init:
            self.assertTrue(obs.init_sentry('bot'))
            self.assertTrue(obs.init_sentry('bot'))
        init.assert_called_once()
        kwargs = init.call_args.kwargs
        self.assertFalse(kwargs['send_default_pii'])
        self.assertFalse(kwargs['include_local_variables'])
        self.assertEqual(kwargs['max_request_body_size'], 'never')
        self.assertEqual(kwargs['environment'], 'production')
        self.assertEqual(kwargs['release'], 'abc123')
        self.assertEqual(kwargs['traces_sample_rate'], 0.0)
        self.assertIs(kwargs['before_send'], obs.before_send)

    def test_web_app_starts_without_dsn(self):
        from leadgen.web import create_app
        with patch.dict(os.environ, {'SENTRY_DSN': ''}), patch('sentry_sdk.init') as init:
            create_app({'SECRET_KEY': 'x' * 40, 'DB_FACTORY': lambda: None})
            init.assert_not_called()


if __name__ == '__main__':
    unittest.main()
