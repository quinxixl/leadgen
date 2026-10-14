import asyncio
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
from cryptography.fernet import Fernet

from leadgen import telegram_accounts as ta
from leadgen.app import db_open
from leadgen.telegram_accounts import SessionCipher, TelegramAccountError


class SessionEncryption(unittest.TestCase):
    def test_roundtrip_and_wrong_key(self):
        cipher = SessionCipher(Fernet.generate_key().decode())
        token = cipher.encrypt({'session': 'secret', 'phone': '+79990000000'})
        self.assertNotIn('secret', token)
        self.assertNotIn('+7999', token)
        self.assertEqual(cipher.decrypt(token)['session'], 'secret')
        with self.assertRaises(TelegramAccountError):
            SessionCipher(Fernet.generate_key().decode()).decrypt(token)

    def test_invalid_key_is_rejected(self):
        with self.assertRaises(TelegramAccountError):
            SessionCipher('short')


CONFIG = {'min_budget': 5000, 'max_age_hours': 72, 'poll_seconds': 300, 'sources': []}
TEXT = 'Нужен сайт для интернет-магазина, бюджет 50 000 ₽'
DIALOGS_SQL = '''CREATE TABLE IF NOT EXISTS user_telegram_dialogs(user_id INTEGER, peer_id INTEGER, title TEXT,
    username TEXT, kind TEXT, enabled INTEGER NOT NULL DEFAULT 0, last_message_at TEXT, updated_at TEXT,
    PRIMARY KEY(user_id, peer_id))'''


class Recorder:
    """Database proxy that logs statements and transaction ends in a shared journal."""
    def __init__(self, db, journal):
        self.db, self.journal, self.connection = db, journal, db.connection

    def execute(self, query, params=()):
        self.journal.append('execute')
        return self.db.execute(query, params)

    def commit(self):
        self.journal.append('commit'); return self.db.commit()

    def close(self):
        self.journal.append('close'); return self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.journal.append('commit' if exc_type is None else 'rollback')
        return self.db.__exit__(exc_type, exc, tb)


def assert_no_open_transaction(case, journal):
    """Every statement logged so far was followed by a commit or rollback."""
    last_execute = max((i for i, item in enumerate(journal) if item == 'execute'), default=-1)
    last_end = max((i for i, item in enumerate(journal) if item in ('commit', 'rollback', 'close')), default=-1)
    case.assertGreater(last_end, last_execute, journal)


def group_event(chat_id=-100500, text=TEXT, msg_id=9, on_get_chat=None, entity=None):
    entity = entity or SimpleNamespace(id=500, username='Orders_Group', broadcast=False, megagroup=True)
    async def get_chat():
        if on_get_chat:
            on_get_chat()
        await asyncio.sleep(0)
        return entity
    return SimpleNamespace(is_private=False, is_group=True, chat_id=chat_id, get_chat=get_chat,
                           message=SimpleNamespace(message=text, id=msg_id, date=datetime.now(timezone.utc)))


class PersonalMonitorDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'monitor.sqlite'
        self.journal = []; self.opened = 0
        self.sql(DIALOGS_SQL,
                 "INSERT INTO user_telegram_dialogs(user_id,peer_id,title,enabled) VALUES(1,-100500,'Заказы',1)",
                 "INSERT INTO user_telegram_dialogs(user_id,peer_id,title,enabled) VALUES(1,-100600,'Выключена',0)")

    def sql(self, *statements):
        db = db_open(self.path)
        try:
            with db:
                for statement in statements:
                    db.execute(statement)
        finally:
            db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def opener(self, target):
        self.opened += 1
        db = db_open(target)
        db.connection.create_function('now', 0, lambda: datetime.now(timezone.utc).isoformat())
        return Recorder(db, self.journal)

    def leads(self):
        db = db_open(self.path)
        try:
            return [row['url'] for row in db.execute('SELECT url FROM leads ORDER BY url')]
        finally:
            db.close()

    def test_no_transaction_or_lock_is_held_while_awaiting_telegram(self):
        store = ta.MonitorStore(self.path, opener=self.opener)
        # Warm the shared connection first, so get_chat happens after real DB work.
        asyncio.run(store.run(lambda db: db.execute('SELECT 1').fetchone()))
        seen = []
        def during_get_chat():
            assert_no_open_transaction(self, self.journal)
            seen.append(store.lock.locked())
        enabled = {(1, -100500)}
        ok = asyncio.run(ta.handle_personal_message(group_event(on_get_chat=during_get_chat), 1, store, enabled, CONFIG))
        store.close()
        self.assertTrue(ok); self.assertEqual(seen, [False])
        assert_no_open_transaction(self, self.journal)
        self.assertEqual(self.leads(), ['https://t.me/Orders_Group/9'])

    def test_connection_is_shared_across_messages_and_off_the_loop(self):
        store = ta.MonitorStore(self.path, opener=self.opener)
        enabled = {(1, -100500), (1, -100600)}
        async def burst():
            loop_thread = threading.get_ident(); threads = []
            def probe(db):
                threads.append(threading.get_ident()); return db.execute('SELECT 1').fetchone()
            await asyncio.gather(*(ta.handle_personal_message(group_event(msg_id=i), 1, store, enabled, CONFIG)
                                   for i in range(1, 6)), store.run(probe))
            # Disabled in DB although still in the (stale) cache: re-checked inside the write transaction.
            stale = await ta.handle_personal_message(group_event(chat_id=-100600, msg_id=77), 1, store, enabled, CONFIG)
            return loop_thread, threads, stale
        loop_thread, threads, stale = asyncio.run(burst())
        store.close()
        self.assertEqual(self.opened, 1)
        self.assertNotEqual(threads, [loop_thread]); self.assertFalse(stale)
        self.assertEqual(len(self.leads()), 5)

    def test_unselected_group_costs_no_database_call(self):
        store = ta.MonitorStore(self.path, opener=self.opener)
        calls = []
        ok = asyncio.run(ta.handle_personal_message(group_event(chat_id=-100999, on_get_chat=lambda: calls.append(1)),
                                                    1, store, {(1, -100500)}, CONFIG))
        store.close()
        self.assertFalse(ok); self.assertEqual((self.opened, calls, self.journal), (0, [], []))

    def test_manager_query_loads_connections_and_enabled_cache(self):
        self.sql('''CREATE TABLE telegram_connections(user_id INTEGER PRIMARY KEY, session_cipher TEXT,
                status TEXT, updated_at TEXT)''',
                 "INSERT INTO telegram_connections VALUES(1,'x','active','t'),(2,'','active','t'),(3,'y','revoked','t')")
        store = ta.MonitorStore(self.path, opener=self.opener)
        rows, enabled = store.call(ta._load_monitor_targets)
        store.close()
        self.assertEqual([row['user_id'] for row in rows], [1]); self.assertEqual(enabled, {(1, -100500)})
        assert_no_open_transaction(self, self.journal)


class DroppedConnection(unittest.TestCase):
    class Conn:
        def __init__(self, fail):
            self.fail, self.closed = fail, False

        def execute(self, query, params=()):
            if self.fail:
                self.closed = True  # psycopg marks a connection the server killed as closed
                raise psycopg.OperationalError('server closed the connection unexpectedly')
            return SimpleNamespace(fetchone=lambda: {'ok': 1})

        def commit(self): pass

        def rollback(self):
            if self.closed:
                raise psycopg.OperationalError('the connection is closed')

        def close(self):
            self.closed = True

    def test_killed_connection_is_replaced_and_work_retried(self):
        from leadgen.database import Database
        conns = [self.Conn(fail=True), self.Conn(fail=False)]; made = []
        def opener(target):
            made.append(conns.pop(0)); return Database(made[-1], 'postgres', target)
        store = ta.MonitorStore('postgresql://test', opener=opener)
        self.assertEqual(store.call(lambda db: db.execute('SELECT 1').fetchone()), {'ok': 1})
        self.assertEqual(store.call(lambda db: db.execute('SELECT 1').fetchone()), {'ok': 1})
        store.close()
        self.assertEqual(len(made), 2); self.assertTrue(made[0].closed)

    def test_gives_up_after_one_retry_and_recovers_later(self):
        from leadgen.database import Database
        conns = [self.Conn(fail=True), self.Conn(fail=True), self.Conn(fail=False)]
        store = ta.MonitorStore('postgresql://test', opener=lambda t: Database(conns.pop(0), 'postgres', t))
        with self.assertRaises(psycopg.OperationalError):
            store.call(lambda db: db.execute('SELECT 1').fetchone())
        self.assertEqual(asyncio.run(store.run(lambda db: db.execute('SELECT 1').fetchone())), {'ok': 1})
        store.close()

    def test_ordinary_errors_keep_the_connection(self):
        opened = []
        store = ta.MonitorStore(':memory:', opener=lambda t: opened.append(1) or ta_db())
        with self.assertRaises(ValueError):
            store.call(lambda db: (_ for _ in ()).throw(ValueError('bug')))
        store.call(lambda db: db.execute('SELECT 1').fetchone())
        store.close()
        self.assertEqual(len(opened), 1)


def ta_db():
    from leadgen.database import Database
    connection = sqlite3.connect(':memory:'); connection.row_factory = sqlite3.Row
    return Database(connection, 'sqlite', ':memory:')


class FlowsEndReadTransaction(unittest.TestCase):
    """Sync connection flows commit their read before a slow Telegram call."""
    class Db:
        def __init__(self, journal, row):
            self.journal, self.row = journal, row

        def execute(self, query, params=()):
            self.journal.append('execute'); return SimpleNamespace(fetchone=lambda: self.row)

        def commit(self):
            self.journal.append('commit')

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.journal.append('commit'); return False

    def setUp(self):
        self.cipher = SessionCipher(Fernet.generate_key().decode()); self.journal = []
        journal = self.journal; case = self

        class Gateway:
            def __getattr__(self, name):
                def call(*args):
                    assert_no_open_transaction(case, journal); journal.append('gateway:' + name)
                    return {'dialogs': [], 'logout': True, 'wait_qr': {'pending': True, 'state': {}},
                            'resend': {'delivery': 'по SMS'}}.get(name, {})
                return call
        self.gateway = Gateway()

    def test_gateway_calls_happen_outside_transactions(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        session = {'session_cipher': self.cipher.encrypt({'session': 's'})}
        ta.sync_dialogs(self.Db(self.journal, session), 1, self.cipher, self.gateway)
        ta.disconnect(self.Db(self.journal, session), 1, self.cipher, self.gateway)
        qr = {'stage': 'qr', 'expires_at': future, 'attempts': 0, 'state_cipher': self.cipher.encrypt({'token': 't'})}
        ta.wait_qr_connection(self.Db(self.journal, qr), 1, self.cipher, self.gateway)
        code = {'stage': 'code', 'expires_at': future, 'attempts': 0,
                'state_cipher': self.cipher.encrypt({'next_delivery': 'по SMS', 'resend_after': 0})}
        ta.resend_connection(self.Db(self.journal, code), 1, self.cipher, self.gateway)
        ta.begin_qr_connection(self.Db(self.journal, None), 1, self.cipher, self.gateway)
        self.assertEqual([item for item in self.journal if item.startswith('gateway:')],
                         ['gateway:dialogs', 'gateway:logout', 'gateway:wait_qr', 'gateway:resend', 'gateway:begin_qr'])



class StopLoop(BaseException):
    pass


class FakeClient:
    def __init__(self, authorized=True, connect=None, run=None):
        self.authorized, self.connect_impl, self.run_impl = authorized, connect, run
        self.disconnects = 0

    async def connect(self):
        if self.connect_impl:
            await self.connect_impl()

    async def is_user_authorized(self):
        return self.authorized

    async def run_until_disconnected(self):
        if self.run_impl:
            await self.run_impl()

    async def disconnect(self):
        self.disconnects += 1


class PersonalClientLifecycle(unittest.TestCase):
    def test_disconnects_when_session_is_revoked(self):
        client = FakeClient(authorized=False)
        with self.assertRaises(TelegramAccountError):
            asyncio.run(ta.serve_personal_client(client, 1))
        self.assertEqual(client.disconnects, 1)

    def test_disconnects_on_network_error(self):
        async def fail():
            raise ConnectionError('reset')
        client = FakeClient(run=fail)
        with self.assertRaises(ConnectionError):
            asyncio.run(ta.serve_personal_client(client, 1))
        self.assertEqual(client.disconnects, 1)

    def test_disconnects_when_cancelled_during_connect(self):
        client = FakeClient(connect=lambda: asyncio.sleep(30))

        async def scenario():
            task = asyncio.create_task(ta.serve_personal_client(client, 1))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(scenario())
        self.assertEqual(client.disconnects, 1)

    def test_ready_hook_failure_keeps_connection(self):
        async def broken_hook():
            raise RuntimeError('db down')
        client = FakeClient()
        with redirect_quiet():
            asyncio.run(ta.serve_personal_client(client, 1, on_ready=broken_hook))
        self.assertEqual(client.disconnects, 1)


def redirect_quiet():
    import contextlib
    import io
    return contextlib.redirect_stdout(io.StringIO())


class PersonalConnectionBackoff(unittest.TestCase):
    def run_keep(self, outcomes, clock=None):
        recorded, sleeps, logs = [], [], []
        outcomes = list(outcomes)

        async def attempt():
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

        async def record(user_id, text):
            recorded.append((user_id, text))

        async def sleep(seconds):
            sleeps.append(seconds)
            if not outcomes:
                raise StopLoop()
        try:
            asyncio.run(ta.keep_personal_connection(7, attempt, record, sleep=sleep,
                                                    clock=clock or (lambda: 0.0), log=logs.append))
        except StopLoop:
            pass
        return recorded, sleeps, logs

    def test_transient_errors_retry_with_backoff_and_never_raise(self):
        recorded, sleeps, _ = self.run_keep([ConnectionError('x'), OSError('y'), asyncio.TimeoutError(), None])
        self.assertEqual(sleeps, [5, 10, 20, 40])
        self.assertEqual(len(recorded), 4)
        self.assertTrue(all(user_id == 7 and text.startswith('Временная ошибка') for user_id, text in recorded))

    def test_revoked_session_propagates_without_retry(self):
        from telethon import errors
        for error in (TelegramAccountError('revoked'), errors.AuthKeyUnregisteredError(request=None),
                      errors.SessionRevokedError(request=None)):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self.run_keep([error, None])

    def test_flood_wait_waits_requested_seconds(self):
        from telethon import errors
        flood = errors.FloodWaitError(request=None, capture=120)
        _, sleeps, _ = self.run_keep([flood])
        self.assertEqual(sleeps, [121])

    def test_backoff_is_capped_and_reset_after_healthy_run(self):
        _, sleeps, _ = self.run_keep([OSError()] * 8)
        self.assertEqual(sleeps, [5, 10, 20, 40, 80, 160, 300, 300])
        now = [0.0]

        def clock():
            now[0] += 700  # every attempt ran longer than HEALTHY_RUN_SECONDS
            return now[0]
        _, sleeps, _ = self.run_keep([OSError()] * 3, clock=clock)
        self.assertEqual(sleeps, [5, 5, 5])

    def test_record_failure_does_not_stop_retries(self):
        sleeps = []

        async def attempt():
            raise ConnectionError()

        async def record(user_id, text):
            raise RuntimeError('db down')

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise StopLoop()
        with self.assertRaises(StopLoop):
            asyncio.run(ta.keep_personal_connection(1, attempt, record, sleep=sleep, log=lambda _: None))
        self.assertEqual(sleeps, [5, 10])


class ManagerLoop(unittest.TestCase):
    def test_iteration_exception_does_not_kill_the_loop(self):
        calls, sleeps, reports = [], [], []

        async def iteration():
            calls.append(1)
            if len(calls) == 1:
                raise KeyError('boom')

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise StopLoop()
        with self.assertRaises(StopLoop):
            asyncio.run(ta.run_forever(iteration, interval=15, sleep=sleep, log=lambda _: None, report=reports.append))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [15, 15, 15])
        self.assertEqual([type(exc) for exc in reports], [KeyError])
