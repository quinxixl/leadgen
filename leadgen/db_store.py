"""One long-lived database connection used off the asyncio event loop by Telegram monitors."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor


def _connection_lost(db, exc):
    """True when the connection behind `db` is unusable and must be replaced."""
    connection = getattr(db, 'connection', None)
    if getattr(connection, 'closed', False) or getattr(connection, 'broken', False):
        return True
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


class MonitorStore:
    """One database connection shared by a group of Telegram monitors (personal monitors, shared reader).

    The Supavisor session pool is small, so monitors never open a connection per Telegram
    message. All work runs on a single worker thread: off the event loop, and the psycopg
    connection is never used from two threads at once. Each unit of work is one short
    transaction, committed or rolled back before control returns to the event loop, so no
    transaction (and no checked-out connection) ever stays open across an await. A
    connection the server closed (idle_in_transaction_session_timeout, pooler restart,
    network) is replaced and the unit of work retried once."""

    def __init__(self, db_target, opener=None, thread_name='personal-monitors-db'):
        self.db_target, self.opener = db_target, opener
        self.db = None
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)

    def _drop(self):
        db, self.db = self.db, None
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    def call(self, work):
        """Run work(db) in one transaction on the shared connection (blocking)."""
        with self.lock:
            for attempt in (0, 1):
                if self.db is None:
                    if self.opener is None:
                        from .app import db_open
                        self.opener = db_open
                    self.db = self.opener(self.db_target)
                db = self.db
                try:
                    with db:
                        return work(db)
                except Exception as exc:
                    if not _connection_lost(db, exc):
                        raise
                    self._drop()
                    if attempt:
                        raise

    def submit(self, work):
        """Run work(db) on the DB thread from synchronous code and wait for it (blocking).

        Unlike call(), the connection stays on the one DB thread, which SQLite requires."""
        return self.executor.submit(self.call, work).result()

    async def run(self, work):
        """Run work(db) on the DB thread; the transaction has ended when this returns."""
        return await asyncio.get_running_loop().run_in_executor(self.executor, self.call, work)

    def close(self):
        def drop():
            with self.lock:
                self._drop()
        try:
            self.executor.submit(drop).result(timeout=10)
        except Exception:
            pass
        self.executor.shutdown(wait=False)
