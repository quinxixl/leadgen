"""Database connections for production Postgres and isolated SQLite tests."""
import os
import re
import sqlite3
import time
from pathlib import Path


SCHEMA_SQLITE = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS leads (
    url TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, first_seen TEXT NOT NULL,
    sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
    origin_user_id INTEGER
);
CREATE INDEX IF NOT EXISTS leads_fp ON leads(fingerprint);
CREATE TABLE IF NOT EXISTS health (
    source TEXT PRIMARY KEY, checked TEXT, ok INTEGER, count INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS telegram_sources (
    username TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
    segment TEXT, priority INTEGER, selection_group TEXT,
    original_members INTEGER, members INTEGER, online INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', enabled INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT, last_message_at TEXT, check_reason TEXT,
    last_message_id INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS telegram_sources_enabled ON telegram_sources(enabled,status,priority);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''


def database_url():
    value=os.environ.get('DATABASE_URL','').strip()
    if not value:
        raise ValueError('DATABASE_URL не задан; укажите строку подключения Supabase Postgres')
    if not re.match(r'^postgres(?:ql)?://',value):
        raise ValueError('DATABASE_URL должен быть строкой подключения PostgreSQL')
    return value


def transient_errors():
    """Database errors a long-running worker survives: roll back (or reconnect) and retry later.

    psycopg.OperationalError covers QueryCanceled (statement_timeout), lost connections, admin shutdown,
    deadlocks and serialization failures; InterfaceError is raised on an already closed connection.
    Programming errors (ProgrammingError, IntegrityError, DataError...) are deliberately not included."""
    try:
        import psycopg
    except ImportError:
        return ()
    return (psycopg.OperationalError,psycopg.InterfaceError)


class Database:
    def __init__(self,connection,backend,target):
        self.connection,self.backend,self.target=connection,backend,target
        self._depth=0

    def execute(self,query,params=()):
        if self.backend=='postgres':
            query=query.replace('?', '%s')
        return self.connection.execute(query,params)

    def commit(self):
        return self.connection.commit()

    def close(self):
        return self.connection.close()

    def release(self):
        """End the transaction psycopg opened implicitly for SELECTs outside `with db:`.

        The connection is not autocommit, so any read leaves the session "idle in transaction" holding
        ACCESS SHARE locks (which block migrations) until the next commit. Call this before network calls,
        sleeps and long loops. Inside `with db:` it does nothing, so multi-statement writes stay atomic.
        It commits rather than rolls back: writes code left outside `with db:` would be committed by the
        next `with db:` block anyway, and committing a read-only transaction is equivalent to a rollback."""
        if self.backend!='postgres' or self._depth:
            return
        info=getattr(self.connection,'info',None)
        status=getattr(info,'transaction_status',None)
        status=getattr(status,'name',status)
        if status=='INTRANS':
            self.connection.commit()
        elif status=='INERROR':
            self.connection.rollback()

    def recover(self):
        """After a database error outside `with db:`: roll back, or reopen a closed/broken connection.

        The Database object keeps its identity, so every holder (controller, loops) sees the new connection.
        Raises RuntimeError when Postgres is still unreachable; the caller retries later."""
        self._depth=0
        connection=self.connection
        if self.backend!='postgres':
            connection.rollback();return
        if not (getattr(connection,'closed',False) or getattr(connection,'broken',False)):
            try:
                connection.rollback();return
            except transient_errors():
                pass
        try:connection.close()
        except Exception:pass
        self.connection=db_open(self.target).connection

    def __enter__(self):
        self._depth+=1
        return self

    def __exit__(self,exc_type,exc_value,traceback):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self._depth=max(0,self._depth-1)
        return False


def db_open(target=None):
    """Open Supabase Postgres. A Path is accepted only for migration/tests."""
    if isinstance(target,Path):
        target.parent.mkdir(exist_ok=True,parents=True)
        connection=sqlite3.connect(target)
        connection.row_factory=sqlite3.Row
        connection.executescript(SCHEMA_SQLITE)
        return Database(connection,'sqlite',str(target))
    dsn=target or database_url()
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise ValueError('Установите зависимости: pip install -r requirements.txt') from None
    for attempt in range(3):
        try:
            connection=psycopg.connect(dsn,row_factory=dict_row,
                options='-c search_path=leadgen,public',connect_timeout=10)
            break
        except psycopg.OperationalError:
            if attempt==2:raise RuntimeError('Supabase Postgres: не удалось подключиться после 3 попыток') from None
            time.sleep(1.5*(attempt+1))
    return Database(connection,'postgres',dsn)
