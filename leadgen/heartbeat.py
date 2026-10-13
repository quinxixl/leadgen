"""Worker liveness: a throttled heartbeat row in `settings` and the Docker healthcheck that reads it.

The worker loop calls beat(db_target) every iteration; at most once per INTERVAL it upserts
KEY on its own autocommit connection, so it never joins (or commits) the loop's transaction.
`python -m leadgen.app healthcheck` does one primary-key lookup under a short statement_timeout.
"""
import json
import os
import socket
import sqlite3
import time
from datetime import datetime

from .core import UTC
from .database import Database

KEY = 'worker:heartbeat'  # telegram_monitor keeps its reader heartbeat under a different key
INTERVAL = 30             # seconds between writes
DEFAULT_MAX_AGE = 300     # healthcheck: older heartbeat means the loop is stuck or gone
TIMEOUT_MS = 3000

UPSERT = '''INSERT INTO settings(key,value) VALUES(?,?)
    ON CONFLICT(key) DO UPDATE SET value=excluded.value'''


def max_age():
    try:
        return max(INTERVAL, int(os.environ.get('WORKER_HEARTBEAT_MAX_AGE', '')))
    except ValueError:
        return DEFAULT_MAX_AGE


def connect(target, timeout_ms=TIMEOUT_MS):
    """Separate autocommit connection with a single fast attempt (db_open retries for ~30 s)."""
    target = str(target)
    if not target.startswith(('postgres://', 'postgresql://')):
        connection = sqlite3.connect(target, timeout=timeout_ms/1000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return Database(connection, 'sqlite', target)
    import psycopg
    from psycopg.rows import dict_row
    connection = psycopg.connect(target, row_factory=dict_row, autocommit=True, connect_timeout=5,
                                 options=f'-c search_path=leadgen,public -c statement_timeout={int(timeout_ms)}'
                                         f' -c lock_timeout={int(timeout_ms)}')
    return Database(connection, 'postgres', target)


class Heartbeat:
    def __init__(self, target, interval=INTERVAL, clock=time.time, monotonic=time.monotonic,
                 opener=connect, log=print):
        self.target, self.interval, self.clock, self.monotonic = target, interval, clock, monotonic
        self.opener, self.log = opener, log
        self.db, self.next_at = None, None

    def __call__(self):
        """Write if due. Never raises: a DB hiccup must not stop lead delivery."""
        now = self.monotonic()
        if self.next_at is not None and now < self.next_at:
            return False
        self.next_at = now + self.interval
        at = self.clock()
        payload = {'at': at, 'at_iso': datetime.fromtimestamp(at, UTC).isoformat(),
                   'host': socket.gethostname(), 'pid': os.getpid()}
        try:
            if self.db is None:
                self.db = self.opener(self.target)
            self.db.execute(UPSERT, (KEY, json.dumps(payload)))
            return True
        except Exception as exc:
            self.log('Heartbeat не записан: '+type(exc).__name__, flush=True)
            self.close()
            return False

    def close(self):
        db, self.db = self.db, None
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


_beats = {}


def beat(target):
    """One-line hook for the worker loop: throttled per database target."""
    if target not in _beats:
        _beats[target] = Heartbeat(target)
    return _beats[target]()


def read(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row['value'])
        return value if isinstance(value, dict) and isinstance(value.get('at'), (int, float)) else None
    except (TypeError, ValueError):
        return None


def check(db, now=None, host=None, limit=None):
    """(healthy, message). A heartbeat from another container (e.g. the one this deploy
    replaced) counts as missing, so `up --wait` only passes once the new worker loops."""
    now = time.time() if now is None else now
    host = socket.gethostname() if host is None else host
    limit = max_age() if limit is None else limit
    value = read(db)
    if value is None:
        return False, 'нет heartbeat воркера'
    if value.get('host') != host:
        return False, f"heartbeat от другого контейнера ({value.get('host')}); воркер здесь ещё не стартовал"
    age = int(now - value['at'])
    if age > limit:
        return False, f'heartbeat устарел: {age} с (порог {limit} с)'
    return True, f'ok: heartbeat {max(0, age)} с назад'


def status(db, now=None):
    """Heartbeat facts for the human `status` command."""
    value = read(db)
    if value is None:
        return {'state': 'never_started', 'age_seconds': None}
    age = max(0, int((time.time() if now is None else now) - value['at']))
    return {'state': 'stale' if age > max_age() else 'running', 'age_seconds': age,
            'at': value.get('at_iso'), 'host': value.get('host')}


def healthcheck(target):
    """Exit code for Docker: 0 healthy, 1 otherwise. No Sentry: DB outages would spam it."""
    try:
        db = connect(target)
        try:
            ok, message = check(db)
        finally:
            db.close()
    except Exception as exc:
        ok, message = False, 'ошибка проверки: '+type(exc).__name__
    print(message, flush=True)
    return 0 if ok else 1
