"""Worker heartbeat, Docker healthcheck and cheap lead status counts."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from leadgen import app, heartbeat as hb
from leadgen.database import db_open


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class HeartbeatTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'leads.sqlite3'
        self.db = db_open(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def stored(self):
        return hb.read(self.db)


class WriteTests(HeartbeatTestCase):
    def make(self, **kwargs):
        self.mono, self.wall = Clock(50.0), Clock(1_700_000_000.0)
        return hb.Heartbeat(str(self.path), interval=30, clock=self.wall, monotonic=self.mono, **kwargs)

    def test_first_call_writes_heartbeat(self):
        beat = self.make()
        self.assertTrue(beat())
        value = self.stored()
        self.assertEqual(value['at'], 1_700_000_000.0)
        self.assertEqual(value['host'], hb.socket.gethostname())
        beat.close()

    def test_throttled_within_interval(self):
        beat = self.make()
        self.assertTrue(beat())
        self.wall.now += 29; self.mono.now += 29
        self.assertFalse(beat())
        self.assertEqual(self.stored()['at'], 1_700_000_000.0)
        self.wall.now += 1; self.mono.now += 1
        self.assertTrue(beat())
        self.assertEqual(self.stored()['at'], 1_700_000_030.0)
        beat.close()

    def test_own_connection_never_commits_worker_transaction(self):
        beat = self.make()
        self.assertTrue(beat())
        self.assertIsNot(beat.db.connection, self.db.connection)
        self.assertFalse(self.db.connection.in_transaction)
        self.assertFalse(beat.db.connection.in_transaction)  # autocommitted, nothing held open
        other = db_open(self.path)
        try:
            self.assertIsNotNone(hb.read(other))
        finally:
            other.close()
        beat.close()

    def test_errors_are_swallowed_and_reconnect_later(self):
        logs, calls = [], []

        def opener(target):
            calls.append(target)
            if len(calls) == 1:
                raise RuntimeError('db down')
            return hb.connect(target)
        beat = self.make(opener=opener, log=lambda *a, **k: logs.append(a[0]))
        self.assertFalse(beat())
        self.assertEqual(logs, ['Heartbeat не записан: RuntimeError'])
        self.mono.now += 1
        self.assertFalse(beat())  # failed attempts are throttled too
        self.mono.now += 30
        self.assertTrue(beat())
        self.assertEqual(len(calls), 2)
        beat.close()

    def test_module_beat_is_throttled_per_target(self):
        target = str(self.path)
        with patch.dict(hb._beats, clear=True):
            self.assertTrue(hb.beat(target))
            self.assertFalse(hb.beat(target))
            hb._beats[target].close()


class CheckTests(HeartbeatTestCase):
    def put(self, value):
        with self.db:
            self.db.execute(hb.UPSERT, (hb.KEY, value if isinstance(value, str) else json.dumps(value)))

    def test_fresh(self):
        self.put({'at': 1000.0, 'host': 'box'})
        ok, message = hb.check(self.db, now=1100.0, host='box', limit=300)
        self.assertTrue(ok, message)

    def test_stale(self):
        self.put({'at': 1000.0, 'host': 'box'})
        ok, message = hb.check(self.db, now=1301.0, host='box', limit=300)
        self.assertFalse(ok)
        self.assertIn('устарел', message)

    def test_missing(self):
        ok, message = hb.check(self.db, now=1000.0, host='box', limit=300)
        self.assertFalse(ok)
        self.assertIn('нет heartbeat', message)

    def test_previous_container_counts_as_missing(self):
        self.put({'at': 1000.0, 'host': 'old-container'})
        ok, message = hb.check(self.db, now=1001.0, host='new-container', limit=300)
        self.assertFalse(ok)
        self.assertIn('другого контейнера', message)

    def test_corrupt_value_is_missing(self):
        self.put('not json')
        self.assertFalse(hb.check(self.db, now=1000.0, host='box', limit=300)[0])

    def test_threshold_from_env(self):
        with patch.dict('os.environ', {'WORKER_HEARTBEAT_MAX_AGE': '600'}):
            self.assertEqual(hb.max_age(), 600)
        with patch.dict('os.environ', {'WORKER_HEARTBEAT_MAX_AGE': 'x'}):
            self.assertEqual(hb.max_age(), hb.DEFAULT_MAX_AGE)
        with patch.dict('os.environ', {'WORKER_HEARTBEAT_MAX_AGE': '1'}):
            self.assertEqual(hb.max_age(), hb.INTERVAL)

    def test_healthcheck_exit_codes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hb.healthcheck(self.path), 1)  # fresh deploy: no heartbeat yet
            beat = hb.Heartbeat(str(self.path))
            beat(); beat.close()
            self.assertEqual(hb.healthcheck(self.path), 0)
            self.assertEqual(hb.healthcheck(Path(self.tmp.name)/'missing'/'x.sqlite3'), 1)
        self.assertIn('ok: heartbeat', out.getvalue())
        self.assertIn('ошибка проверки', out.getvalue())

    def test_cli_healthcheck_exits_without_config(self):
        hb.Heartbeat(str(self.path))()
        with patch.object(sys, 'argv', ['leadgen.app', 'healthcheck', '--db', str(self.path)]), \
                patch.object(app, 'env_load'), redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            app.main()
        self.assertEqual(raised.exception.code, 0)


class FakePostgres:
    backend = 'postgres'

    def __init__(self, row):
        self.row, self.queries = row, []

    def execute(self, query, params=()):
        self.queries.append(query)
        return self

    def fetchone(self):
        return self.row


class LeadCountTests(HeartbeatTestCase):
    def test_sqlite_counts_exactly(self):
        with self.db:
            for i, status in enumerate(['ready', 'ready', 'sent']):
                self.db.execute("INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?)",
                                (f'u{i}', f'f{i}', '{}', status, '', '2026-10-13'))
        self.assertEqual(app.lead_counts(self.db), ({'ready': 2, 'sent': 1}, 'exact'))

    def test_postgres_uses_planner_statistics_not_a_scan(self):
        db = FakePostgres({'vals': ['sent', 'review'], 'freqs': [0.75, 0.25], 'reltuples': 1000.0})
        self.assertEqual(app.lead_counts(db), ({'sent': 750, 'review': 250}, 'approximate'))
        self.assertIn('pg_stats', db.queries[0])
        self.assertNotIn('GROUP BY', db.queries[0])

    def test_postgres_without_statistics(self):
        counts, mode = app.lead_counts(FakePostgres({'vals': None, 'freqs': None, 'reltuples': -1.0}))
        self.assertEqual(counts, {})
        self.assertIn('--exact', mode)

    def test_status_reports_worker_heartbeat(self):
        out = io.StringIO()
        with patch('leadgen.source_registry.read_count', return_value=0), redirect_stdout(out):
            app.status(self.db)
        data = json.loads(out.getvalue())
        self.assertEqual(data['counts_mode'], 'exact')
        self.assertEqual(data['worker']['state'], 'never_started')


if __name__ == '__main__':
    unittest.main()
