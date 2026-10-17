"""Shared Telegram reader: public channels, auto-registration, supervisor, heartbeat, QR setup. No network."""
import asyncio
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telethon import errors, events
from telethon.tl import types

from leadgen import telegram_monitor as tm
from leadgen.adapters import SourceError, error_label, parse
from leadgen.app import db_open, status
from leadgen.core import UTC
from leadgen.source_registry import (APPROVED_REASON, MODERATION_REASON, NOT_JOINED_REASON, REJECTED_REASON,
                                     apply_monitor_policy, approve_source, moderation_queue, reject_source)

CONFIG = {'min_budget': 5000, 'max_age_hours': 72, 'poll_seconds': 300, 'sources': []}
TEXT = 'Нужен сайт для интернет-магазина, бюджет 50 000 ₽'


def chat(username, title='Канал', broadcast=True, **extra):
    return SimpleNamespace(username=username, title=title, broadcast=broadcast, megagroup=not broadcast, **extra)


def event(entity, text=TEXT, msg_id=7, cached=False, channel=True, fail=None):
    async def get_chat():
        if fail:
            raise fail
        return entity
    return SimpleNamespace(chat=entity if cached else None, get_chat=get_chat, is_private=False,
                           is_group=not channel, is_channel=True,
                           message=SimpleNamespace(message=text, id=msg_id, date=datetime.now(UTC)))


class FakeClient:
    def __init__(self, authorized=True, dialogs=(), incoming=(), connect_error=None):
        self.authorized, self.dialogs, self.incoming, self.connect_error = authorized, dialogs, incoming, connect_error
        self.handlers = []; self.disconnected = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def is_user_authorized(self):
        return self.authorized

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    def iter_dialogs(self):
        async def gen():
            for dialog in self.dialogs:
                yield dialog
        return gen()

    async def run_until_disconnected(self):
        for item in self.incoming:
            for callback, _ in self.handlers:
                await callback(item)

    def is_connected(self):
        return not self.disconnected

    async def disconnect(self):
        self.disconnected = True


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'reader.sqlite'
        self.db = db_open(self.path); self.logs = []

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def source(self, username):
        return self.db.execute('SELECT * FROM telegram_sources WHERE username=?', (username,)).fetchone()

    def heartbeat(self):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (tm.HEARTBEAT_KEY,)).fetchone()
        return json.loads(row['value']) if row else None


class Selection(unittest.TestCase):
    def test_channel_posts_pass_the_incoming_filter(self):
        builder = events.NewMessage(incoming=True); builder.resolved = True
        post = types.Message(id=7, peer_id=types.PeerChannel(1234), date=datetime.now(UTC), message=TEXT, out=False, post=True)
        built = events.NewMessage.build(types.UpdateNewChannelMessage(message=post, pts=1, pts_count=1))
        self.assertTrue(builder.filter(built))
        self.assertTrue(built.is_channel); self.assertFalse(built.is_group)
        own = types.Message(id=8, peer_id=types.PeerChannel(1234), date=datetime.now(UTC), message=TEXT, out=True, post=True)
        self.assertFalse(builder.filter(events.NewMessage.build(types.UpdateNewChannelMessage(message=own, pts=2, pts_count=1))))

    def test_public_username_rules(self):
        self.assertEqual(tm.public_username(chat('Dev_Orders')), 'dev_orders')
        collectible = chat(None, usernames=[SimpleNamespace(username='old', active=False), SimpleNamespace(username='Shop', active=True)])
        self.assertEqual(tm.public_username(collectible), 'shop')
        self.assertIsNone(tm.public_username(chat(None)))
        ev = SimpleNamespace(is_private=False, is_group=True, is_channel=True)
        self.assertIsNone(tm.realtime_group_username(ev, SimpleNamespace(username='ivan', first_name='Иван')))
        self.assertIsNone(tm.realtime_group_username(SimpleNamespace(is_private=True), chat('somebody')))
        self.assertIsNone(tm.realtime_group_username(ev, None))


class Registration(DbCase):
    def test_joined_public_channel_is_registered_and_survives_policy(self):
        with self.db:
            self.assertTrue(tm.register_joined_chat(self.db, 'news_orders', 'Заказы', 'channel'))
            self.db.execute('''INSERT INTO telegram_sources(username,url,title,selection_group,status,enabled,check_reason,metadata)
                VALUES('old_chan','https://t.me/old_chan','Old','Требуют проверки','inactive',0,'нет публикаций','{"risks":"x"}')''')
            self.assertTrue(tm.register_joined_chat(self.db, 'old_chan', 'Old', 'channel'))  # marker added
            self.assertFalse(tm.register_joined_chat(self.db, 'news_orders', 'Заказы', 'channel'))
            self.assertFalse(tm.register_joined_chat(self.db, 'old_chan', 'Old', 'channel'))
        row = self.source('news_orders')
        # A chat the account joined on its own waits for the admin and is not read yet.
        self.assertEqual((row['status'], row['enabled'], row['selection_group']), ('moderation', 0, 'joined'))
        self.assertEqual(row['check_reason'], MODERATION_REASON)
        old = self.source('old_chan')
        # An existing row keeps enabled/status/check_reason; only the joined marker is added.
        self.assertEqual((old['status'], old['enabled'], old['check_reason']), ('inactive', 0, 'нет публикаций'))
        self.assertEqual(json.loads(old['metadata']), {'risks': 'x', 'joined': True, 'kind': 'channel'})
        self.assertEqual(apply_monitor_policy(self.db), 0)  # the policy never approves a chat
        self.assertEqual(self.source('news_orders')['enabled'], 0)
        self.assertTrue(approve_source(self.db, 'news_orders'))
        # Channels have no online count; once approved, the joined marker keeps them enabled.
        self.assertEqual(apply_monitor_policy(self.db), 1)
        self.assertEqual(self.source('news_orders')['enabled'], 1)

    def test_disabled_source_is_never_reenabled_by_the_reader(self):
        with self.db:
            # Disabled by an admin (joined, active) and by the registry (not joined at the time).
            self.db.execute('''INSERT INTO telegram_sources(username,url,title,selection_group,status,enabled,check_reason,metadata)
                VALUES('blocked_chat','https://t.me/blocked_chat','Blocked','joined','active',0,'отключён вручную',
                '{"joined": true, "kind": "group"}')''')
            for username in ('synced_chat', 'live_chat'):
                self.db.execute('''INSERT INTO telegram_sources(username,url,title,selection_group,status,enabled,check_reason,metadata)
                    VALUES(?,?,?,'Потенциальные клиенты','active',0,?,'{"risks":""}')''',
                    (username, 'https://t.me/'+username, username, NOT_JOINED_REASON))
        dialogs = [SimpleNamespace(is_user=False, entity=chat(name, name, broadcast=False))
                   for name in ('Synced_Chat', 'Blocked_Chat')]
        self.assertEqual(asyncio.run(tm.sync_dialogs(FakeClient(dialogs=dialogs), self.path, tm.new_state())), (2, 1))
        state = tm.new_state()
        for name in ('Live_Chat', 'Blocked_Chat'):
            ok = asyncio.run(tm.process_event(event(chat(name, name), msg_id=3), self.path, CONFIG, state, self.logs.append))
            self.assertFalse(ok, name)
        expected = {'blocked_chat': 'отключён вручную', 'synced_chat': NOT_JOINED_REASON, 'live_chat': NOT_JOINED_REASON}
        for username, reason in expected.items():
            row = self.source(username)
            self.assertEqual((row['enabled'], row['status'], row['check_reason']), (0, 'active', reason), username)
            self.assertIs(json.loads(row['metadata'])['joined'], True)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM leads').fetchone()['n'], 0)
        self.assertEqual(state['ingested'], 0)
        self.assertFalse(any('ждёт одобрения' in line for line in self.logs))

    def test_new_chat_waits_for_moderation_and_is_not_ingested(self):
        state = tm.new_state()
        ok = asyncio.run(tm.process_event(event(chat('New_Chat', 'Новый чат')), self.path, CONFIG, state, self.logs.append))
        self.assertFalse(ok)
        row = self.source('new_chat')
        self.assertEqual((row['status'], row['enabled'], row['check_reason']), ('moderation', 0, MODERATION_REASON))
        self.assertEqual(row['last_message_id'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM leads').fetchone()['n'], 0)
        self.assertEqual(state['ingested'], 0)
        self.assertIn('@new_chat ждёт одобрения в /admin', self.logs[-1])
        self.assertEqual([r['username'] for r in moderation_queue(self.db)], ['new_chat'])
        # The catalog policy does not enable it either.
        apply_monitor_policy(self.db)
        self.assertEqual(self.source('new_chat')['enabled'], 0)

    def test_approved_chat_is_read(self):
        state = tm.new_state(); known = set()
        self.assertFalse(asyncio.run(tm.process_event(event(chat('Ok_Chat'), msg_id=1), self.path, CONFIG, state, self.logs.append, known)))
        self.assertTrue(approve_source(self.db, 'ok_chat'))
        self.assertFalse(approve_source(self.db, 'ok_chat'))  # only a waiting chat can be approved
        row = self.source('ok_chat')
        self.assertEqual((row['status'], row['enabled'], row['check_reason']), ('active', 1, APPROVED_REASON))
        self.assertEqual(moderation_queue(self.db), [])
        self.assertTrue(asyncio.run(tm.process_event(event(chat('Ok_Chat'), msg_id=2), self.path, CONFIG, state, self.logs.append, known)))
        self.assertEqual(self.db.execute('SELECT url FROM leads').fetchone()['url'], 'https://t.me/ok_chat/2')

    def test_rejected_chat_stays_disabled_and_is_not_queued_again(self):
        state = tm.new_state()
        dialogs = [SimpleNamespace(is_user=False, entity=chat('Spam_Chat', broadcast=False))]
        self.assertEqual(asyncio.run(tm.sync_dialogs(FakeClient(dialogs=dialogs), self.path, state)), (1, 1))
        self.assertTrue(reject_source(self.db, 'spam_chat'))
        self.assertFalse(reject_source(self.db, 'spam_chat'))
        self.assertFalse(approve_source(self.db, 'spam_chat'))  # a rejected chat is not waiting any more
        # A fresh reader process (empty known set) syncs dialogs and receives messages again.
        self.assertEqual(asyncio.run(tm.sync_dialogs(FakeClient(dialogs=dialogs), self.path, state)), (1, 0))
        self.assertFalse(asyncio.run(tm.process_event(event(chat('Spam_Chat'), msg_id=5), self.path, CONFIG, state, self.logs.append)))
        apply_monitor_policy(self.db)
        row = self.source('spam_chat')
        self.assertEqual((row['status'], row['enabled'], row['check_reason']), ('rejected', 0, REJECTED_REASON))
        self.assertEqual(moderation_queue(self.db), [])
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM leads').fetchone()['n'], 0)

    def test_known_chat_skips_registration(self):
        known = set(); state = tm.new_state(); calls = []
        original = tm.register_joined_chat

        def counting(*args, **kwargs):
            calls.append(args[1])
            return original(*args, **kwargs)
        with mock.patch.object(tm, 'register_joined_chat', counting):
            for msg_id in (1, 2, 3):
                ev = event(chat('Busy_Chat', 'Занятой'), msg_id=msg_id)
                self.assertFalse(asyncio.run(tm.process_event(ev, self.path, CONFIG, state, self.logs.append, known)))
            approve_source(self.db, 'busy_chat')
            for msg_id in (4, 5, 6):
                ev = event(chat('Busy_Chat', 'Занятой'), msg_id=msg_id)
                self.assertTrue(asyncio.run(tm.process_event(ev, self.path, CONFIG, state, self.logs.append, known)))
            dialogs = [SimpleNamespace(is_user=False, entity=chat('Busy_Chat', 'Занятой'))]
            self.assertEqual(asyncio.run(tm.sync_dialogs(FakeClient(dialogs=dialogs), self.path, state, known=known)), (1, 0))
        self.assertEqual(calls, ['busy_chat'])
        self.assertEqual(known, {'busy_chat'})
        self.assertEqual(state['ingested'], 3)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM leads').fetchone()['n'], 3)
        self.assertEqual(sum('ждёт одобрения' in line for line in self.logs), 1)

    def approved(self, username, title, kind='channel'):
        with self.db:
            tm.register_joined_chat(self.db, username, title, kind)
        self.assertTrue(approve_source(self.db, username))

    def test_uncached_chat_is_fetched_and_post_ingested(self):
        self.approved('fresh_orders', 'Свежие заказы')
        state = tm.new_state()
        ok = asyncio.run(tm.process_event(event(chat('Fresh_Orders', 'Свежие заказы')), self.path, CONFIG, state, self.logs.append))
        self.assertTrue(ok)
        lead = self.db.execute('SELECT url,payload FROM leads').fetchone()
        self.assertEqual(lead['url'], 'https://t.me/fresh_orders/7')
        self.assertEqual(json.loads(lead['payload'])['source'], 'Telegram: Свежие заказы')
        row = self.source('fresh_orders')
        self.assertEqual((row['enabled'], row['check_reason'], row['last_message_id']), (1, 'получено в реальном времени', 7))
        self.assertEqual(self.heartbeat()['ingested'], 1)

    def test_private_chat_and_failed_get_chat_are_ignored(self):
        state = tm.new_state()
        self.assertFalse(asyncio.run(tm.process_event(event(chat(None, 'Закрытый')), self.path, CONFIG, state, self.logs.append)))
        failing = event(None, fail=ConnectionError('dc down'))
        self.assertFalse(asyncio.run(tm.process_event(failing, self.path, CONFIG, state, self.logs.append)))
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM leads').fetchone()['n'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM telegram_sources').fetchone()['n'], 0)
        self.assertIn('ConnectionError', self.logs[-1])


class Supervisor(unittest.TestCase):
    def run_supervised(self, outcomes):
        state = tm.new_state(); sleeps = []; logs = []; beats = []
        def attempt():
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        tm.supervise(attempt, state, heartbeat=lambda: beats.append(state['state']), sleep=sleeps.append,
                     log=logs.append, max_attempts=len(outcomes))
        return state, sleeps, logs, beats

    def test_restarts_with_backoff_and_flood_wait(self):
        flood = errors.FloodWaitError(request=None, capture=42)
        state, sleeps, logs, _ = self.run_supervised([RuntimeError('boom'), None, flood, OSError('reset')])
        self.assertEqual(sleeps, [5, 10, 43])  # the last attempt ends the test loop without sleeping
        self.assertEqual(state['restarts'], 3)
        self.assertIn('RuntimeError: boom', logs[0])
        self.assertTrue(any('соединение с Telegram закрыто' in line for line in logs))
        self.assertEqual(state['state'], 'backoff'); self.assertIn('OSError', state['last_error'])

    def test_unauthorized_session_backs_off_ten_minutes(self):
        revoked = errors.AuthKeyUnregisteredError(request=None)
        state, sleeps, logs, beats = self.run_supervised([tm.ReaderUnauthorized(), revoked])
        self.assertEqual(sum(sleeps), 600); self.assertEqual(max(sleeps), 60)
        self.assertEqual(state['state'], 'unauthorized'); self.assertFalse(state['authorized'])
        self.assertIn('Сессия служебного аккаунта не авторизована. Выполните setup-telegram', logs[0])
        self.assertIn('AuthKeyUnregisteredError', state['last_error'])
        self.assertGreaterEqual(beats.count('unauthorized'), 10)

    def test_alerts_only_unexpected_errors_and_unauthorized_once(self):
        from unittest import mock
        flood = errors.FloodWaitError(request=None, capture=3)
        revoked = errors.AuthKeyUnregisteredError(request=None)
        with mock.patch('leadgen.observability.capture_exception') as capture:
            self.run_supervised([RuntimeError('boom'), None, flood, ConnectionError('reset'),
                                 revoked, tm.ReaderUnauthorized(), None])
        calls = [(type(c.args[0]).__name__, c.kwargs['reader_state']) for c in capture.call_args_list]
        self.assertEqual(calls, [('RuntimeError', 'backoff'), ('AuthKeyUnregisteredError', 'unauthorized')])
        self.assertTrue(all(c.kwargs['component'] == 'telegram_reader' for c in capture.call_args_list))

    def test_secrets_are_masked(self):
        text = tm.safe_error(RuntimeError('postgresql://u:pw@db/x hash 0123456789abcdef0123456789abcdef phone +79991234567'))
        for secret in ('pw@db', '0123456789abcdef0123456789abcdef', '79991234567'):
            self.assertNotIn(secret, text)

    def test_reader_thread_is_restarted_after_it_dies(self):
        now = [0.0]; runs = []; logs = []
        keeper = tm.ReaderThread(lambda: runs.append(1), clock=lambda: now[0], restart_delay=60, log=logs.append)
        self.assertEqual(keeper.ensure(), 'started'); keeper.thread.join(2)
        self.assertIsNone(keeper.ensure())  # dead, but restart is rate-limited
        now[0] = 61
        self.assertEqual(keeper.ensure(), 'restarted'); keeper.thread.join(2)
        self.assertEqual(len(runs), 2)
        self.assertTrue(any('поток чтения завершился' in line for line in logs))


class Monitor(DbCase):
    def test_run_monitor_recovers_reads_channels_and_beats(self):
        public = chat('Orders_Channel', 'Заказы IT')
        dialogs = [SimpleNamespace(is_user=False, entity=public),
                   SimpleNamespace(is_user=False, entity=chat(None, 'Приватная группа', broadcast=False)),
                   SimpleNamespace(is_user=True, entity=SimpleNamespace(username='person', first_name='P'))]
        with self.db:
            tm.register_joined_chat(self.db, 'orders_channel', 'Заказы IT', 'channel')
        approve_source(self.db, 'orders_channel')
        clients = [FakeClient(connect_error=ConnectionError('network')),
                   FakeClient(dialogs=dialogs, incoming=[event(public, msg_id=11)])]
        state = tm.run_monitor(self.path, CONFIG, client_factory=lambda: clients.pop(0), sleep=lambda s: None,
                               max_attempts=2, log=self.logs.append)
        self.assertEqual(state['restarts'], 1)
        self.assertEqual(state['chats'], 1); self.assertEqual(state['ingested'], 1)
        self.assertTrue(any('Мониторинг Telegram запущен' in line for line in self.logs))
        self.assertIsNotNone(self.source('orders_channel'))
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM telegram_sources').fetchone()['n'], 1)
        self.assertEqual(self.db.execute('SELECT url FROM leads').fetchone()['url'], 'https://t.me/orders_channel/11')
        info = tm.reader_status(self.db)
        self.assertEqual(info['realtime_sources_24h'], 1); self.assertEqual(info['joined_sources'], 1)
        self.assertIsNotNone(info['last_event_at']); self.assertLess(info['heartbeat_age_seconds'], 5)

    def test_unauthorized_session_is_visible_in_status(self):
        tm.run_monitor(self.path, CONFIG, client_factory=lambda: FakeClient(authorized=False), sleep=lambda s: None,
                       max_attempts=1, log=self.logs.append)
        out = io.StringIO()
        with redirect_stdout(out):
            status(self.db)
        reader = json.loads(out.getvalue())['telegram_reader']
        self.assertEqual(reader['state'], 'unauthorized'); self.assertFalse(reader['connected'])
        self.assertIn('setup-telegram', reader['last_error'])
        self.assertIn('session_file', reader)

    def test_stale_and_missing_heartbeat(self):
        self.assertEqual(tm.reader_status(self.db)['state'], 'never_started')
        tm.write_heartbeat(self.db, tm.new_state() | {'state': 'running', 'connected': True},
                           now=datetime.now(UTC)-timedelta(minutes=10))
        info = tm.reader_status(self.db)
        self.assertEqual(info['state'], 'stale'); self.assertFalse(info['connected'])


class FakeQR:
    def __init__(self, outcomes):
        self.outcomes = outcomes; self.round = 0; self.timeouts = []
        self._resp = types.auth.LoginToken(expires=datetime.now(UTC), token=b'first')

    @property
    def url(self):
        return f'tg://login?token={self.round}'

    async def wait(self, timeout=None):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def recreate(self):
        self.round += 1
        self._resp = types.auth.LoginToken(expires=datetime.now(UTC), token=b'next')


class QrSetup(unittest.TestCase):
    def test_qr_refreshes_token_and_asks_cloud_password(self):
        user = SimpleNamespace(first_name='Служебный', last_name=None)
        qr = FakeQR([TimeoutError(), errors.SessionPasswordNeededError(request=None)])
        passwords = ['wrong', 'right']; shown = []

        class Client:
            async def qr_login(self):
                return qr

            async def sign_in(self, password):
                if password != 'right':
                    raise errors.PasswordHashInvalidError(request=None)
                return user
        with redirect_stdout(io.StringIO()):
            result = asyncio.run(tm.qr_login(Client(), show=shown.append, ask_password=lambda: passwords.pop(0)))
        self.assertIs(result, user)
        self.assertEqual(shown, ['tg://login?token=0', 'tg://login?token=1'])
        self.assertTrue(all(10 <= t <= 60 for t in qr.timeouts))  # expired/skewed clock still waits

    def test_login_replaces_revoked_session_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)/'telegram_reader'
            (Path(tmp)/'telegram_reader.session').write_text('revoked')

            class Client:
                def __init__(self, path):
                    self.path = Path(path+'.session')
                    if not self.path.exists():
                        self.path.write_text('fresh')
                async def connect(self): pass
                async def is_user_authorized(self): return self.path.read_text() == 'authorized'
                async def disconnect(self): pass
                async def get_me(self): return SimpleNamespace(first_name='Old')

            async def method(client):
                client.path.write_text('authorized')
                return SimpleNamespace(first_name='New')
            user, replaced = asyncio.run(tm.login_session(Client, method, session))
            self.assertTrue(replaced); self.assertEqual(user.first_name, 'New')
            self.assertEqual((Path(tmp)/'telegram_reader.session').read_text(), 'authorized')
            self.assertFalse((Path(tmp)/'telegram_reader.new.session').exists())
            # Already authorized: kept as is.
            user, replaced = asyncio.run(tm.login_session(Client, method, session))
            self.assertFalse(replaced); self.assertEqual(user.first_name, 'Old')


FL_CARD = '''<div id="project-item5512345" class="b-post">
  <h2 class="b-post__title"><a href="/projects/5512345/razrabotka-sayta.html">Разработка сайта для студии</a></h2>
  <div class="b-post__price">25 000 ₽</div>
  <div class="b-post__txt">Нужен лендинг с формой заявки и интеграцией с CRM.</div>
  <div class="b-post__foot">Проект · 5 октября</div>
</div>
<div id="project-item5512346" class="b-post">
  <h2><a href="/projects/5512346/vakansiya.html">Python-разработчик</a></h2>
  <div class="b-post__txt">Ищем в штат.</div><div class="b-post__foot">Вакансия · 2 часа назад</div>
</div>'''

FL_MAINTENANCE = '''<html><head><title>Удаленная работа (фри-ланс) на Free-lance.ru</title></head>
<body><h1>На сайте проводятся технические работы.</h1><p>В ближайшее время FL.ru будет доступен для посещения.</p></body></html>'''


class FlParsing(unittest.TestCase):
    SOURCE = {'kind': 'fl', 'name': 'FL.ru', 'url': 'https://www.fl.ru/projects/'}

    def test_project_cards(self):
        now = datetime(2026, 10, 11, 12, tzinfo=UTC)
        first, second = parse(self.SOURCE, FL_CARD.encode(), now)
        self.assertEqual(first.url, 'https://www.fl.ru/projects/5512345/razrabotka-sayta.html')
        self.assertEqual(first.title, 'Разработка сайта для студии')
        self.assertEqual(first.budget_text, '25 000 ₽'); self.assertEqual(first.kind, 'order')
        self.assertTrue(first.published.startswith('2026-10-05'))
        self.assertEqual(second.kind, 'vacancy')

    def test_maintenance_page_has_a_clear_error(self):
        with self.assertRaises(SourceError) as caught:
            parse(self.SOURCE, FL_MAINTENANCE.encode())
        self.assertIn('технические работы', str(caught.exception))
        self.assertTrue(error_label(caught.exception).startswith('SourceError: FL.ru: на сайте технические работы'))
        with self.assertRaises(SourceError) as blocked:
            parse(self.SOURCE, b'<html><form action="/captcha/check"></form></html>')
        self.assertIn('капча', str(blocked.exception))
        self.assertEqual(error_label(ValueError('secret')), 'ValueError')


if __name__ == '__main__':
    unittest.main()
