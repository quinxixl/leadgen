"""Telegram error alerts: formatting, scrubbing, dedup, hourly cap, no-op and never raising."""
import logging
import os
import unittest
from unittest.mock import patch

from leadgen import observability as obs

BOT_TOKEN = '1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ'
GROQ_KEY = 'gsk_9fJk2LmN4pQr6StUv8WxYz0AbCdEfGh'


def boom(text='сбой'):
    raise ValueError(text)


def make_exc(text='сбой'):
    try:
        boom(text)
    except ValueError as exc:
        return exc


class AlertTests(unittest.TestCase):
    def setUp(self):
        obs._reset_alerts()
        self.sent = []
        self.post = patch.object(obs, '_post_telegram', side_effect=lambda chat, text: self.sent.append((chat, text)))
        self.post.start()
        self.env = patch.dict(os.environ, {'ERROR_ALERT_CHAT_ID': '111, 222', 'ALERT_ON_START': '0'})
        self.env.start()

    def tearDown(self):
        self.post.stop()
        self.env.stop()
        obs._reset_alerts()

    def test_noop_without_chat_id(self):
        with patch.dict(os.environ, {'ERROR_ALERT_CHAT_ID': ''}):
            self.assertFalse(obs.init_alerts('web', sync=True))
        self.assertFalse(obs.alert_exception(make_exc()))
        obs.capture_exception(make_exc())
        self.assertEqual(self.sent, [])
        self.assertFalse([h for h in logging.getLogger().handlers if isinstance(h, obs.AlertLogHandler)])

    def test_no_fallback_to_other_admin_ids(self):
        with patch.dict(os.environ, {'ERROR_ALERT_CHAT_ID': '', 'TELEGRAM_CHAT_ID': '555', 'ADMIN_TELEGRAM_IDS': '777'}):
            self.assertFalse(obs.init_alerts('bot', sync=True))
        obs.capture_exception(make_exc())
        self.assertEqual(self.sent, [])

    def test_capture_exception_sends_formatted_alert_to_all_chats(self):
        self.assertTrue(obs.init_alerts('bot', sync=True))
        obs.capture_exception(make_exc('не удалось'), user_id=42, component='parser', source='fl')
        self.assertEqual([chat for chat, _ in self.sent], ['111', '222'])
        text = self.sent[0][1]
        self.assertTrue(text.startswith('🔴 Ошибка в bot\nValueError\nне удалось'))
        self.assertIn('component: parser', text)
        self.assertIn('source: fl', text)
        self.assertIn('user_id: 42', text)
        self.assertIn('tests/test_alerts.py', text.replace('leadgen/', ''))
        self.assertIn(' boom', text)
        self.assertIn('МСК', text)
        self.assertLessEqual(len(text), obs.ALERT_MAX_CHARS)

    def test_everything_scrubbed(self):
        obs.init_alerts('web', sync=True)
        secret = (f'owner@example.com +7 999 123-45-67 bot{BOT_TOKEN} {GROQ_KEY} '
                  'postgresql://user:hunter2pass@db/x token=abcdefgh123')
        obs.capture_exception(make_exc(secret), user_id='not-a-number', password='p4ss', api_key='abc', note=secret)
        text = self.sent[0][1]
        for leaked in ('owner@example.com', '123-45-67', BOT_TOKEN, GROQ_KEY, 'hunter2pass', 'abcdefgh123', 'p4ss',
                       'not-a-number'):
            self.assertNotIn(leaked, text)
        self.assertIn('[email]', text)
        self.assertIn('[phone]', text)

    def test_long_message_truncated(self):
        obs.init_alerts('web', sync=True)
        obs.capture_exception(make_exc('x' * 10000), component='y' * 10000)
        self.assertLessEqual(len(self.sent[0][1]), obs.ALERT_MAX_CHARS)

    def test_dedup_window_and_repeat_count(self):
        obs.init_alerts('web', sync=True)
        clock = [1000.0]
        with patch.object(obs.time, 'monotonic', side_effect=lambda: clock[0]):
            for _ in range(4):
                obs.capture_exception(make_exc())
            self.assertEqual(len(self.sent), 2)  # one alert, two chats
            clock[0] += obs.ALERT_WINDOW - 1
            obs.capture_exception(make_exc())
            self.assertEqual(len(self.sent), 2)
            clock[0] += 2
            obs.capture_exception(make_exc())
        self.assertEqual(len(self.sent), 4)
        self.assertIn('повторилась 4 раз за 10 мин', self.sent[-1][1])
        self.assertNotIn('повторилась', self.sent[0][1])

    def test_different_fingerprints_not_deduplicated(self):
        obs.init_alerts('web', sync=True)
        obs.capture_exception(make_exc())
        try:
            {}['x']
        except KeyError as exc:
            obs.capture_exception(exc)
        self.assertEqual(len(self.sent), 4)

    def test_global_hourly_cap(self):
        obs.init_alerts('web', sync=True)
        clock = [5000.0]
        with patch.object(obs.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(obs, 'fingerprint', side_effect=lambda exc: str(exc)):
            for i in range(obs.ALERT_HOURLY_CAP + 5):
                obs.capture_exception(make_exc('e%d' % i))
            self.assertEqual(len(self.sent), obs.ALERT_HOURLY_CAP * 2)
            clock[0] += 3600
            obs.capture_exception(make_exc('later'))
        self.assertEqual(len(self.sent), obs.ALERT_HOURLY_CAP * 2 + 2)

    def test_never_raises(self):
        obs.init_alerts('web', sync=True)
        with patch.object(obs, '_post_telegram', side_effect=OSError('network down')), patch('builtins.print'):
            self.assertTrue(obs.alert_exception(make_exc()))
        with patch.object(obs, 'format_alert', side_effect=RuntimeError('bug')):
            self.assertFalse(obs.alert_exception(make_exc('other')))
        self.assertIsNone(obs.capture_exception(make_exc('third')) if not obs.enabled() else None)

    def test_error_log_records_alert(self):
        obs.init_alerts('web', sync=True)
        log = logging.getLogger('leadgen.test_alerts')
        log.propagate = True
        try:
            boom('из лога')
        except ValueError:
            log.exception('Exception on /x [GET]')
        log.error('plain error for +7 999 123-45-67')
        log.warning('just a warning')
        texts = [t for c, t in self.sent if c == '111']
        self.assertEqual(len(texts), 2)
        self.assertIn('из лога', texts[0])
        self.assertIn('logger: leadgen.test_alerts', texts[0])
        self.assertIn('ERROR в логе', texts[1])
        self.assertNotIn('123-45-67', texts[1])

    def test_background_sender_does_not_block(self):
        obs.init_alerts('web')
        self.assertTrue(obs.alert_exception(make_exc()))
        obs.flush_alerts(5)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(obs._alerts['thread'].daemon)

    def test_start_message_only_when_enabled(self):
        obs.init_alerts('web', sync=True)
        self.assertEqual(self.sent, [])
        obs._reset_alerts()
        with patch.dict(os.environ, {'ALERT_ON_START': '1', 'SENTRY_RELEASE': 'abc123'}):
            obs.init_alerts('bot', sync=True)
        self.assertTrue(self.sent[0][1].startswith('✅ bot запущен (abc123)'))

    def test_cli_test_reports_channels(self):
        with patch.dict(os.environ, {'SENTRY_DSN': ''}), patch('leadgen.app.env_load'), \
                patch('builtins.print') as printed:
            self.assertTrue(obs.send_test_event())
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn('owner@example.com', self.sent[0][1])
        self.assertIn('Telegram', ' '.join(str(c.args[0]) for c in printed.call_args_list))

    def test_ops_alert_is_sent_synchronously_to_every_chat(self):
        with patch('leadgen.app.env_load'):
            self.assertTrue(obs.send_ops_alert('Сбой  leadfinder-backup.service\n на сервере'))
        self.assertEqual([chat for chat, _ in self.sent], ['111', '222'])
        self.assertIn('Сбой leadfinder-backup.service на сервере', self.sent[0][1])

    def test_ops_alert_without_chat_id_or_text_is_not_sent(self):
        with patch('leadgen.app.env_load'), patch('builtins.print'):
            self.assertFalse(obs.send_ops_alert('   '))
            with patch.dict(os.environ, {'ERROR_ALERT_CHAT_ID': ''}):
                obs._reset_alerts()
                self.assertFalse(obs.send_ops_alert('Сбой'))
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
