"""Per-process Postgres connection pool of the web app (review item П2)."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from leadgen import database
from leadgen.database import PooledDatabase, db_pooled, pool_settings, web_pool
from leadgen.web import create_app


class FakeConnection:
    def __init__(self,status='IDLE',broken=False):
        self.status,self.closed,self.broken=status,False,broken
        self.rollbacks=self.commits=0
        self.queries=[]

    @property
    def info(self):
        return SimpleNamespace(transaction_status=SimpleNamespace(name=self.status))

    def execute(self,query,params=()):
        self.queries.append(query)
        self.status='INTRANS'
        return SimpleNamespace(fetchone=lambda:None,fetchall=lambda:[])

    def rollback(self):
        self.rollbacks+=1;self.status='IDLE'

    def commit(self):
        self.commits+=1;self.status='IDLE'


class FakePool:
    conninfo='postgresql://pool.test/db'
    timeout=10.0
    max_size=2

    def __init__(self,connections=(),timeout_error=False):
        self.free=list(connections)
        self.returned=[]
        self.timeout_error=timeout_error

    def getconn(self):
        if self.timeout_error or not self.free:
            from psycopg_pool import PoolTimeout
            raise PoolTimeout('couldn\'t get a connection after 10.00 sec')
        return self.free.pop(0)

    def putconn(self,connection):
        self.returned.append(connection)


class PoolSettings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(pool_settings({}),{'min_size':1,'max_size':4,'timeout':10.0,'connection_kwargs':{}})

    def test_custom_values_and_prepare_threshold(self):
        settings=pool_settings({'DB_POOL_MAX':'5','DB_POOL_MIN':'3','DB_POOL_TIMEOUT':'2.5','DB_PREPARE_THRESHOLD':'None'})
        self.assertEqual((settings['min_size'],settings['max_size'],settings['timeout']),(3,5,2.5))
        self.assertEqual(settings['connection_kwargs'],{'prepare_threshold':None})
        self.assertEqual(pool_settings({'DB_PREPARE_THRESHOLD':'0'})['connection_kwargs'],{'prepare_threshold':0})
        self.assertEqual(pool_settings({'DB_PREPARE_THRESHOLD':' '})['connection_kwargs'],{})

    def test_min_defaults_to_at_most_max(self):
        self.assertEqual(pool_settings({'DB_POOL_MAX':'1'})['min_size'],1)
        self.assertEqual(pool_settings({'DB_POOL_MIN':'0'})['min_size'],0)

    def test_invalid_values_fail_fast(self):
        for env in ({'DB_POOL_MAX':'two'},{'DB_POOL_MAX':'0'},{'DB_POOL_MIN':'-1'},
                    {'DB_POOL_MIN':'3','DB_POOL_MAX':'2'},{'DB_POOL_TIMEOUT':'0'},{'DB_PREPARE_THRESHOLD':'off'}):
            with self.subTest(env=env),self.assertRaises(ValueError):pool_settings(env)


class PoolLifecycle(unittest.TestCase):
    def setUp(self):
        patcher=patch.object(database,'_pool_entry',None)
        patcher.start();self.addCleanup(patcher.stop)

    def test_pool_created_lazily_once_per_process(self):
        with patch.object(database,'_create_pool',side_effect=lambda dsn:FakePool()) as create:
            create_app({'TESTING':True,'SECRET_KEY':'test-'*10})
            create.assert_not_called()
            first=web_pool('postgresql://127.0.0.1/x')
            self.assertIs(web_pool(),first)
            create.assert_called_once_with('postgresql://127.0.0.1/x')
            with patch('leadgen.database.os.getpid',return_value=os.getpid()+1):
                child=web_pool('postgresql://127.0.0.1/x')
            self.assertIsNot(child,first)
            self.assertEqual(create.call_count,2)

    def test_fork_hook_forgets_parent_pool(self):
        database._pool_entry=(os.getpid(),FakePool())
        database._forget_pool()
        self.assertIsNone(database._pool_entry)

    def test_close_pool_at_exit_only_in_owner_process(self):
        pool=SimpleNamespace(closed=0)
        pool.close=lambda timeout:setattr(pool,'closed',pool.closed+1)
        database._close_pool(pool,os.getpid()+1)
        self.assertEqual(pool.closed,0)
        database._close_pool(pool,os.getpid())
        self.assertEqual(pool.closed,1)

    def test_exhausted_pool_raises_clear_runtime_error(self):
        pool=FakePool(timeout_error=True)
        with patch.object(database,'web_pool',return_value=pool):
            with self.assertRaisesRegex(RuntimeError,'нет свободного соединения в пуле.*DB_POOL_MAX=2'):db_pooled()


class PooledDatabaseTests(unittest.TestCase):
    def test_close_rolls_back_open_transaction_then_returns_connection(self):
        connection=FakeConnection();pool=FakePool([connection])
        db=PooledDatabase(pool.getconn(),pool)
        db.execute('SELECT ?',(1,))
        self.assertEqual(connection.queries,['SELECT %s'])
        db.close()
        self.assertEqual(connection.rollbacks,1)
        self.assertEqual(pool.returned,[connection])

    def test_close_idle_connection_skips_rollback_and_double_close_is_noop(self):
        connection=FakeConnection();pool=FakePool()
        db=PooledDatabase(connection,pool)
        db.close();db.close()
        self.assertEqual(connection.rollbacks,0)
        self.assertEqual(pool.returned,[connection])

    def test_close_returns_connection_even_if_rollback_fails(self):
        import psycopg
        connection=FakeConnection(status='INERROR');pool=FakePool()
        def broken_rollback():raise psycopg.OperationalError('server closed the connection')
        connection.rollback=broken_rollback
        with self.assertLogs('leadgen.database','WARNING'):PooledDatabase(connection,pool).close()
        self.assertEqual(pool.returned,[connection])

    def test_recover_swaps_broken_connection_for_pooled_one(self):
        broken,fresh=FakeConnection(broken=True),FakeConnection()
        pool=FakePool([fresh])
        db=PooledDatabase(broken,pool)
        db.recover()
        self.assertIs(db.connection,fresh)
        self.assertEqual(pool.returned,[broken])

    def test_recover_rolls_back_healthy_connection(self):
        connection=FakeConnection(status='INERROR');pool=FakePool()
        db=PooledDatabase(connection,pool)
        db.recover()
        self.assertIs(db.connection,connection)
        self.assertEqual((connection.rollbacks,pool.returned),(1,[]))

    def test_release_and_transaction_context_still_work(self):
        connection=FakeConnection();db=PooledDatabase(connection,FakePool())
        with db:db.execute('UPDATE settings SET value=? WHERE key=?',('1','x'))
        self.assertEqual(connection.commits,1)
        db.execute('SELECT 1');db.release()
        self.assertEqual(connection.commits,2)


class FlaskTeardown(unittest.TestCase):
    def test_request_returns_connection_to_pool_with_rollback(self):
        connection=FakeConnection();pool=FakePool([connection])
        app=create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'SESSION_COOKIE_SECURE':False,
                        'DB_FACTORY':lambda:PooledDatabase(pool.getconn(),pool)})
        client=app.test_client()
        with client.session_transaction() as session:session['user_id']=1
        client.get('/app')
        self.assertTrue(any('app_users' in query for query in connection.queries))
        self.assertEqual(pool.returned,[connection])
        self.assertEqual(connection.rollbacks,1)
        self.assertEqual(connection.status,'IDLE')

    def test_default_factory_is_pooled(self):
        app=create_app({'TESTING':True,'SECRET_KEY':'test-'*10})
        self.assertIs(app.config['DB_FACTORY'],db_pooled)


DSN=os.environ.get('WEB_TEST_DATABASE_URL','')


@unittest.skipUnless(DSN,'WEB_TEST_DATABASE_URL is not configured')
class RealPool(unittest.TestCase):
    def setUp(self):
        from urllib.parse import urlsplit
        url=urlsplit(DSN)
        if url.hostname!='127.0.0.1':
            raise RuntimeError('Use only the disposable local test database')
        env={'DB_POOL_MAX':'1','DB_POOL_MIN':'1','DB_POOL_TIMEOUT':'0.5','DB_PREPARE_THRESHOLD':'none'}
        patchers=(patch.dict(os.environ,env),patch.object(database,'_pool_entry',None))
        for patcher in patchers:
            patcher.start();self.addCleanup(patcher.stop)
        self.pool=web_pool(DSN)
        self.addCleanup(self.pool.close)

    def test_connection_reused_rolled_back_and_exhaustion_reported(self):
        db=db_pooled()
        raw=db.connection
        self.assertIsNone(raw.prepare_threshold)
        self.assertEqual(db.execute('SHOW search_path').fetchone()['search_path'],'leadgen,public')
        db.execute('CREATE TEMP TABLE pool_probe(x int)')
        with self.assertRaisesRegex(RuntimeError,'нет свободного соединения'):db_pooled()
        db.close();db.close()
        again=db_pooled()
        self.assertIs(again.connection,raw)
        self.assertIsNone(again.execute("SELECT to_regclass('pool_probe') AS name").fetchone()['name'])
        again.close()

    def test_broken_connection_replaced_by_pool(self):
        db=db_pooled()
        db.connection.close()
        db.close()
        fresh=db_pooled()
        self.assertEqual(fresh.execute('SELECT 1 AS one').fetchone()['one'],1)
        fresh.close()


if __name__=='__main__':
    unittest.main()
