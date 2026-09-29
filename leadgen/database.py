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
    sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0
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


class Database:
    def __init__(self,connection,backend,target):
        self.connection,self.backend,self.target=connection,backend,target

    def execute(self,query,params=()):
        if self.backend=='postgres':
            query=query.replace('?', '%s')
        return self.connection.execute(query,params)

    def commit(self):
        return self.connection.commit()

    def close(self):
        return self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self,exc_type,exc_value,traceback):
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
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
