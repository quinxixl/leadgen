"""Database connections for production Postgres and isolated SQLite tests."""
import atexit
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path


log=logging.getLogger(__name__)
POOL_MAX_DEFAULT=4
POOL_MIN_DEFAULT=1
POOL_TIMEOUT_DEFAULT=10.0
POOL_CLOSE_TIMEOUT=5.0
SEARCH_PATH_OPTION='-c search_path=leadgen,public'
CONNECT_TIMEOUT=10


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
                options=SEARCH_PATH_OPTION,connect_timeout=CONNECT_TIMEOUT)
            break
        except psycopg.OperationalError:
            if attempt==2:raise RuntimeError('Supabase Postgres: не удалось подключиться после 3 попыток') from None
            time.sleep(1.5*(attempt+1))
    return Database(connection,'postgres',dsn)


def _env_number(environ,name,default,cast,minimum):
    raw=str(environ.get(name,'')).strip()
    if not raw:
        return default
    try:
        value=cast(raw)
    except ValueError:
        raise ValueError(f'{name} должен быть числом, получено {raw!r}') from None
    if value<minimum:
        raise ValueError(f'{name} должен быть не меньше {minimum}, получено {raw!r}')
    return value


def pool_settings(environ=None):
    """Web connection pool settings from DB_POOL_MAX/DB_POOL_MIN/DB_POOL_TIMEOUT/DB_PREPARE_THRESHOLD.

    DB_PREPARE_THRESHOLD=none disables server-side prepared statements, which the Supabase
    transaction pooler (port 6543) does not support; unset keeps psycopg's default."""
    environ=os.environ if environ is None else environ
    max_size=_env_number(environ,'DB_POOL_MAX',POOL_MAX_DEFAULT,int,1)
    min_size=_env_number(environ,'DB_POOL_MIN',min(POOL_MIN_DEFAULT,max_size),int,0)
    if min_size>max_size:
        raise ValueError(f'DB_POOL_MIN ({min_size}) не может быть больше DB_POOL_MAX ({max_size})')
    timeout=_env_number(environ,'DB_POOL_TIMEOUT',POOL_TIMEOUT_DEFAULT,float,0.1)
    connection_kwargs={}
    threshold=str(environ.get('DB_PREPARE_THRESHOLD','')).strip()
    if threshold.lower()=='none':
        connection_kwargs['prepare_threshold']=None
    elif threshold:
        connection_kwargs['prepare_threshold']=_env_number(environ,'DB_PREPARE_THRESHOLD',None,int,0)
    return {'min_size':min_size,'max_size':max_size,'timeout':timeout,'connection_kwargs':connection_kwargs}


class PooledDatabase(Database):
    """A Database whose connection is borrowed from the per-process web pool.

    close() rolls back whatever the request left open and returns the connection to the pool;
    calling it twice is a no-op. recover() swaps a broken connection for a fresh pooled one."""
    def __init__(self,connection,pool):
        super().__init__(connection,'postgres',pool.conninfo)
        self._pool=pool

    def close(self):
        connection,self.connection=self.connection,None
        self._depth=0
        if connection is None:
            return
        try:
            if not connection.closed and connection.info.transaction_status.name!='IDLE':
                connection.rollback()
        except transient_errors():
            log.warning('Postgres: rollback перед возвратом в пул не удался; соединение будет пересоздано',exc_info=True)
        finally:
            # The pool discards closed or broken connections and opens replacements itself.
            self._pool.putconn(connection)

    def recover(self):
        self._depth=0
        connection=self.connection
        if connection is not None and not (connection.closed or connection.broken):
            try:
                connection.rollback();return
            except transient_errors():
                pass
        self.connection=None
        if connection is not None:
            self._pool.putconn(connection)
        self.connection=_checkout(self._pool)


_pool_lock=threading.Lock()
_pool_entry=None


def _forget_pool():
    """In a forked child the parent's pool threads do not exist: start a new pool, leave the parent's sockets alone."""
    global _pool_lock,_pool_entry
    _pool_lock,_pool_entry=threading.Lock(),None


if hasattr(os,'register_at_fork'):
    os.register_at_fork(after_in_child=_forget_pool)


def _close_pool(pool,pid):
    if os.getpid()!=pid:
        return
    try:
        pool.close(timeout=POOL_CLOSE_TIMEOUT)
    except Exception:
        log.warning('Postgres: не удалось закрыть пул соединений',exc_info=True)


def _create_pool(dsn):
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError:
        raise ValueError('Установите зависимости: pip install -r requirements.txt') from None
    settings=pool_settings()
    kwargs={'row_factory':dict_row,'options':SEARCH_PATH_OPTION,'connect_timeout':CONNECT_TIMEOUT,
            **settings['connection_kwargs']}
    pid=os.getpid()
    pool=ConnectionPool(dsn,kwargs=kwargs,min_size=settings['min_size'],max_size=settings['max_size'],
        timeout=settings['timeout'],check=ConnectionPool.check_connection,name=f'web-{pid}',open=False)
    pool.open(wait=False)
    atexit.register(_close_pool,pool,pid)
    log.info('Postgres: пул соединений открыт (pid=%s, min=%s, max=%s, timeout=%ss)',
             pid,settings['min_size'],settings['max_size'],settings['timeout'])
    return pool


def web_pool(dsn=None):
    """The connection pool of this process, created on first use (each gunicorn worker gets its own)."""
    global _pool_entry
    pid=os.getpid()
    entry=_pool_entry
    if entry is not None and entry[0]==pid:
        return entry[1]
    with _pool_lock:
        if _pool_entry is None or _pool_entry[0]!=pid:
            _pool_entry=(pid,_create_pool(dsn or database_url()))
        return _pool_entry[1]


def _checkout(pool):
    from psycopg_pool import PoolTimeout
    try:
        return pool.getconn()
    except PoolTimeout:
        raise RuntimeError(f'Postgres: нет свободного соединения в пуле за {pool.timeout:g} с '
                           f'(DB_POOL_MAX={pool.max_size}) или база недоступна') from None


def db_pooled():
    """Borrow a connection from the per-process web pool. Only the web app uses it; bot and monitors keep db_open()."""
    pool=web_pool()
    return PooledDatabase(_checkout(pool),pool)
