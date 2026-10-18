"""Steady-state database traffic: cadences of the worker loop and the columns/rows the hot paths read."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from leadgen import telegram_accounts as ta
from leadgen import telegram_monitor as tm
from leadgen.app import DUPLICATE_SQL, db_open, find_duplicate, ingest
from leadgen.bot import Cadence, deliver_queues
from leadgen.core import UTC
from leadgen.product import ProductController, _ready_payloads
from test_leadgen import CONFIG, lead
import test_web_postgres as fixtures


class Recording:
    """Database wrapper that records every statement."""
    def __init__(self, db):
        self.db, self.queries = db, []

    def execute(self, query, params=()):
        self.queries.append(' '.join(query.split()))
        return self.db.execute(query, params)

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *args):
        return self.db.__exit__(*args)

    def writes(self):
        return [q for q in self.queries if q.startswith(('INSERT', 'UPDATE', 'DELETE'))]

    def lookups(self):
        return [q for q in self.queries if q.startswith('SELECT url FROM leads')]


class IngestSkipsUnchangedRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db_open(Path(self.tmp.name)/'test.sqlite')

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def ingest(self, item, **kwargs):
        recording = Recording(self.db)
        with self.db:
            written = ingest(recording, item, CONFIG, **kwargs)
        return written, recording

    def test_rescan_of_an_unchanged_lead_neither_looks_up_duplicates_nor_writes(self):
        self.assertTrue(self.ingest(lead())[0])
        written, recording = self.ingest(lead())
        self.assertFalse(written)
        self.assertEqual((recording.writes(), recording.lookups()), ([], []))
        self.assertEqual(len(recording.queries), 1)  # one primary-key lookup

    def test_changed_payload_or_classification_is_still_written(self):
        self.ingest(lead())
        written, recording = self.ingest(lead(text='Нужно создать бота для записи клиентов и оплаты'))
        self.assertTrue(written); self.assertEqual(len(recording.writes()), 1)
        with patch('leadgen.app.classify_for_config', return_value=('rejected', 'направление выключено')):
            self.assertTrue(self.ingest(lead(text='Нужно создать бота для записи клиентов и оплаты'))[0])
        self.assertEqual(self.db.execute('SELECT status FROM leads').fetchone()['status'], 'rejected')

    def test_stored_duplicate_is_rechecked_and_freed_when_the_original_is_gone(self):
        self.ingest(lead())
        self.ingest(lead(source='second', url='https://example.org/2'))
        second = self.db.execute("SELECT status FROM leads WHERE url='https://example.org/2'").fetchone()
        self.assertEqual(second['status'], 'duplicate')
        written, recording = self.ingest(lead(source='second', url='https://example.org/2'))
        self.assertFalse(written); self.assertEqual(len(recording.lookups()), 1)  # unchanged: still a duplicate
        with self.db:
            self.db.execute("DELETE FROM leads WHERE url='https://example.org/1'")
        self.assertTrue(self.ingest(lead(source='second', url='https://example.org/2'))[0])
        second = self.db.execute("SELECT status FROM leads WHERE url='https://example.org/2'").fetchone()
        self.assertEqual(second['status'], 'ready')

    def test_duplicate_lookup_is_sargable_and_respects_origin(self):
        self.assertNotIn(' OR ', DUPLICATE_SQL); self.assertNotIn('CAST', DUPLICATE_SQL)
        self.ingest(lead())
        self.ingest(lead(url='https://t.me/c/1/2'), origin_user_id=7)
        fp = self.db.execute('SELECT fingerprint FROM leads LIMIT 1').fetchone()['fingerprint']
        self.assertEqual(find_duplicate(self.db, fp, 'https://other'), 'https://example.org/1')
        self.assertEqual(find_duplicate(self.db, fp, 'https://other', 7), 'https://t.me/c/1/2')
        self.assertIsNone(find_duplicate(self.db, fp, 'https://other', 8))


class WorkerCadence(unittest.TestCase):
    def test_cadence_runs_first_then_once_per_interval_and_on_wake(self):
        now = [0.0]
        cadence = Cadence(15, clock=lambda: now[0])
        self.assertTrue(cadence.due())
        now[0] = 14; self.assertFalse(cadence.due())
        cadence.wake(); self.assertTrue(cadence.due())
        now[0] = 28; self.assertFalse(cadence.due())
        now[0] = 29; self.assertTrue(cadence.due())

    def test_queues_report_a_full_batch_so_the_loop_drains_it(self):
        with patch('leadgen.integrations.integrations_enabled', return_value=False), \
                patch('leadgen.reminders.deliver_due', return_value=0) as due, \
                patch('leadgen.team_notifications.deliver_assignments', return_value=5):
            self.assertTrue(deliver_queues(object(), print))
            due.return_value = 1
            with patch('leadgen.team_notifications.deliver_assignments', return_value=0):
                self.assertFalse(deliver_queues(object(), print))


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class ControllerSettings(unittest.TestCase):
    def test_offset_and_active_are_not_reread_on_every_iteration(self):
        queries = []

        class Db:
            def execute(self, query, params=()):
                queries.append(query)
                return Cursor({'value': '41'} if 'settings' in query else {'x': 1})

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False
        ui = ProductController(Db(), {}, print)
        for _ in range(5):
            self.assertEqual(ui.get('offset', 0), 41)
            self.assertTrue(ui.get('active', False))
        self.assertEqual(len(queries), 2)
        ui.put('offset', 42)
        self.assertEqual(ui.get('offset', 0), 42)
        self.assertEqual(len(queries), 3)  # only the write
        ui._active_cache = None  # what handle() does after each update
        ui.get('active')
        self.assertEqual(len(queries), 4)


class DeliveryPayloads(unittest.TestCase):
    def test_payloads_are_fetched_in_small_chunks_and_only_while_needed(self):
        fetched = []

        class Db:
            def execute(self, query, params=()):
                if 'FROM leads' in query:
                    fetched.append(list(params))
                    return SimpleNamespace(fetchall=lambda: [{'id': i, 'payload': '{}'} for i in params])
                return SimpleNamespace(fetchall=lambda: [])

            def release(self):
                pass
        chunks = _ready_payloads(Db(), list(range(100, 75, -1)), chunk=10)
        rows, _ = next(chunks)
        self.assertEqual([row['id'] for row in rows], list(range(100, 90, -1)))
        self.assertEqual(len(fetched), 1)  # a pass that sends from the first chunk reads nothing more
        self.assertEqual([len(item) for item in [rows] + [r for r, _ in chunks]], [10, 10, 5])


class ReaderSourceGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'reader.sqlite'
        self.db = db_open(self.path)
        with self.db:
            for i in range(30):
                self.db.execute('''INSERT INTO telegram_sources(username,url,title,status,enabled)
                    VALUES(?,?,?,?,?)''', (f'chat{i}', f'https://t.me/chat{i}', f'Чат {i}', 'active', i % 2))
        self.store = tm.MonitorStore(self.path, thread_name='test-egress-db')

    def tearDown(self):
        self.store.close(); self.db.close(); self.tmp.cleanup()

    def test_refresh_reads_only_chats_the_gate_holds(self):
        gate = tm.SourceGate({'chat1': False, 'chat2': True, 'gone': False})
        with patch.object(tm, 'FLAGS_CHUNK', 2):
            asyncio.run(tm.refresh_sources(self.store, gate, print))
        self.assertEqual(gate.active, {'chat1': True, 'chat2': False})
        empty = tm.SourceGate()
        asyncio.run(tm.refresh_sources(self.store, empty, print))
        self.assertEqual(empty.active, {})

    def test_busy_chat_row_is_rewritten_at_most_every_few_minutes(self):
        now = datetime.now(UTC)
        row = {'check_reason': tm.REALTIME_REASON, 'last_message_at': (now-timedelta(seconds=30)).isoformat()}
        self.assertTrue(tm._marked_recently(row, now.isoformat()))
        self.assertFalse(tm._marked_recently(row | {'last_message_at': (now-timedelta(minutes=6)).isoformat()},
                                             now.isoformat()))
        self.assertFalse(tm._marked_recently(row | {'check_reason': 'одобрено'}, now.isoformat()))
        self.assertFalse(tm._marked_recently(row | {'last_message_at': None}, now.isoformat()))
        self.assertNotIn('metadata', tm.SOURCE_COLUMNS)


class PersonalManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'p.sqlite'
        self.db = db_open(self.path)
        with self.db:
            self.db.execute('''CREATE TABLE telegram_connections(user_id INTEGER PRIMARY KEY, session_cipher TEXT,
                status TEXT, updated_at TEXT)''')
            self.db.execute('CREATE TABLE user_telegram_dialogs(user_id INTEGER, peer_id INTEGER, enabled INTEGER)')
            self.db.execute("INSERT INTO telegram_connections VALUES(1,'c1','active','v1'),(2,'c2','active','v2')")

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_sessions_of_running_unchanged_clients_are_not_reread(self):
        recording = Recording(self.db)
        rows, _ = ta._load_monitor_targets(recording, {1: 'v1', 2: 'old'})
        self.assertEqual({row['user_id']: row['session_cipher'] for row in rows}, {1: None, 2: 'c2'})
        # The poll itself reads no session; only the changed one is fetched by id.
        self.assertIn('SELECT user_id,session_cipher FROM telegram_connections WHERE user_id IN (?)', recording.queries)
        rows, _ = ta._load_monitor_targets(self.db)
        self.assertEqual({row['user_id']: row['session_cipher'] for row in rows}, {1: 'c1', 2: 'c2'})

    def test_recent_dialog_write_is_skipped(self):
        now = datetime.now(UTC)
        self.assertTrue(ta._written_recently(now-timedelta(seconds=10), now))
        self.assertTrue(ta._written_recently((now-timedelta(seconds=10)).isoformat(), now))
        self.assertFalse(ta._written_recently(now-timedelta(minutes=10), now))
        self.assertFalse(ta._written_recently(None, now))


class Scorer(unittest.TestCase):
    def test_scorer_keeps_one_connection_and_backs_off_at_the_daily_limit(self):
        from leadgen import ai_scoring
        opened, waits = [], []

        class Db:
            def release(self):
                pass

            def close(self):
                pass

        def opener(target):
            opened.append(target); return Db()

        def score(db, on_limit=None):
            if len(waits) == 2:
                on_limit()
            return 0

        class Stop:
            def is_set(self):
                return len(waits) >= 3

            def wait(self, seconds):
                waits.append(seconds)
        with patch.object(ai_scoring, 'score_pending', score):
            ai_scoring.run_scorer('dsn', interval=5, stop=Stop(), opener=opener)
        self.assertEqual(opened, ['dsn'])
        self.assertEqual(waits, [5, 5, ai_scoring.LIMIT_PAUSE_SECONDS])


@unittest.skipUnless(fixtures.DSN, 'WEB_TEST_DATABASE_URL is not configured')
class PostgresIngest(unittest.TestCase):
    """jsonb payload equality in the database and the partial dedup index on the typed schema."""
    @classmethod
    def setUpClass(cls):
        fixtures.WebPostgres.setUpClass.__func__(cls)
        fixtures.apply_no_transaction_file('20261018120000_leads_dedup_index.sql')
        fixtures.apply_no_transaction_file('20261018120000_leads_dedup_index.sql')  # idempotent: created ahead by hand

    @classmethod
    def tearDownClass(cls):
        fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)

    def ingest(self, item, **kwargs):
        recording = Recording(self.db)
        with self.db:
            written = ingest(recording, item, CONFIG, **kwargs)
        return written, recording

    def test_unchanged_rescan_is_one_lookup_and_changes_are_written(self):
        self.assertTrue(self.ingest(lead())[0])
        written, recording = self.ingest(lead())
        self.assertFalse(written); self.assertEqual(len(recording.queries), 1)
        changed = 'Нужно создать бота для записи клиентов и оплаты'
        self.assertTrue(self.ingest(lead(text=changed))[0])
        self.ingest(lead(source='second', url='https://example.org/2', text=changed))
        self.ingest(lead(url='https://t.me/c/1/2', text=changed), origin_user_id=self.u)
        rows = {row['url']: row['status'] for row in self.db.execute(
            'SELECT url,status FROM leads WHERE url IN (?,?,?)',
            ('https://example.org/1', 'https://example.org/2', 'https://t.me/c/1/2')).fetchall()}
        self.assertEqual(rows, {'https://example.org/1': 'ready', 'https://example.org/2': 'duplicate',
                                'https://t.me/c/1/2': 'ready'})

    def test_partial_index_matches_the_lookup(self):
        row = self.db.execute("""SELECT indexdef FROM pg_indexes WHERE schemaname='leadgen'
            AND indexname='leads_fp_live'""").fetchone()
        self.assertIn('(fingerprint, origin_user_id)', row['indexdef'])
        with self.db:
            self.db.execute('SET LOCAL enable_seqscan=off')
            plan = '\n'.join(r['QUERY PLAN'] for r in self.db.execute(
                'EXPLAIN '+DUPLICATE_SQL.format(' IS NULL'), ('fp', 'url')).fetchall())
        self.assertIn('leads_fp_live', plan)


if __name__ == '__main__':
    unittest.main()
