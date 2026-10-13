import contextlib
import hashlib
import io
import unittest
from unittest import mock

from leadgen import web_migrate
from leadgen.database import Database
from leadgen.web_migrate import Migration, Settings


class LockError(Exception):
    sqlstate='55P03'


class OtherError(Exception):
    sqlstate='42P01'


class Cursor:
    def __init__(self,row):self.row=row
    def fetchone(self):return self.row
    def fetchall(self):return [] if self.row is None else [self.row]


class FakeConnection:
    """Records (query, params, autocommit) and lets a test inject failures."""

    def __init__(self,applied=None,indexes=None,lock_acquired=True):
        self.calls=[];self.autocommit=False;self.commits=0;self.rollbacks=0
        self.applied=dict(applied or {});self.indexes=dict(indexes or {});self.lock_acquired=lock_acquired
        self.failures={}

    def fail(self,fragment,*errors):
        self.failures.setdefault(fragment,[]).extend(errors)

    def execute(self,query,params=None,prepare=None):
        self.calls.append((query,params,self.autocommit))
        for fragment,errors in self.failures.items():
            if fragment in query and errors:raise errors.pop(0)
        if 'pg_try_advisory_lock' in query:return Cursor({'acquired':self.lock_acquired})
        if 'SELECT checksum FROM' in query:
            return Cursor({'checksum':self.applied[params[0]]} if params[0] in self.applied else None)
        if 'FROM pg_index' in query:
            state=self.indexes.get(params[0])
            if isinstance(state,list):state=state.pop(0) if len(state)>1 else state[0]
            return Cursor(None if state is None else {'valid':state})
        return Cursor(None)

    def commit(self):self.commits+=1
    def rollback(self):self.rollbacks+=1
    def close(self):pass

    def queries(self,fragment):
        return [call for call in self.calls if fragment in call[0]]


def migration(name,sql):
    return Migration(name,sql,hashlib.sha256(sql.encode()).hexdigest(),web_migrate.is_no_transaction(sql))


NOTX='''-- leadgen:no-transaction
-- Indexes for lead queries.
CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_status_idx ON leadgen.leads(status);

-- second one
CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_seen_idx
    ON leadgen.leads(first_seen DESC);
-- trailing comment only
'''
SETTINGS=Settings(attempts=3,retry_base=1,retry_max=10)


class WebMigrateRun(unittest.TestCase):
    def run_migrations(self,connection,migrations,settings=SETTINGS):
        sleep=mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:web_migrate.run(Database(connection,'postgres','test'),migrations,settings,sleep=sleep)
            finally:self.output=out.getvalue()
        return sleep

    def test_lock_failure_rolls_back_and_retries_whole_batch(self):
        connection=FakeConnection()
        connection.fail('CREATE TABLE two',LockError('canceling statement due to lock timeout'))
        sleep=self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()'),migration('b.sql','CREATE TABLE two()')])
        self.assertEqual(len(connection.queries('CREATE TABLE one')),2)
        self.assertEqual(len(connection.queries('CREATE TABLE two')),2)
        self.assertEqual(sleep.call_count,1)
        self.assertGreaterEqual(connection.rollbacks,1)
        self.assertEqual(connection.commits,1)
        self.assertEqual(len(connection.queries('INSERT INTO leadgen.web_schema_versions')),3)
        self.assertTrue(connection.queries('pg_locks'))
        self.assertIn('Попытка 1/3',self.output)
        self.assertEqual(connection.queries("set_config('lock_timeout'")[0][1],('15s',))

    def test_gives_up_after_max_attempts_and_reraises(self):
        connection=FakeConnection()
        connection.fail('CREATE TABLE one',*[LockError('lock timeout') for _ in range(5)])
        with self.assertRaises(LockError):
            sleep=self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()')])
        self.assertEqual(len(connection.queries('CREATE TABLE one')),3)
        self.assertEqual(connection.commits,0)
        self.assertIn('Попытки исчерпаны (3/3)',self.output)
        self.assertTrue(connection.queries('pg_advisory_unlock'))

    def test_backoff_is_exponential_capped_and_jittered(self):
        delays=[]
        settings=Settings(attempts=5,retry_base=2,retry_max=5)
        def always():raise LockError('x')
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(LockError):
            web_migrate.with_lock_retry(always,'t',settings,sleep=delays.append,rand=lambda:1.0)
        self.assertEqual(delays,[2,4,5,5])
        delays.clear()
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(LockError):
            web_migrate.with_lock_retry(always,'t',settings,sleep=delays.append,rand=lambda:0.0)
        self.assertEqual(delays,[1,2,2.5,2.5])

    def test_diagnostics_failure_does_not_mask_lock_error(self):
        connection=FakeConnection()
        connection.fail('CREATE TABLE one',LockError('lock timeout'))
        connection.fail('pg_locks',RuntimeError('permission denied'))
        self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()')])
        self.assertEqual(connection.commits,1)
        self.assertIn('Не удалось получить список блокирующих сессий',self.output)

    def test_non_lock_errors_are_not_retried(self):
        connection=FakeConnection()
        connection.fail('CREATE TABLE one',OtherError('relation does not exist'))
        with self.assertRaises(OtherError):
            sleep=self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()')])
        self.assertEqual(len(connection.queries('CREATE TABLE one')),1)
        self.assertEqual(connection.commits,0)
        self.assertFalse(connection.queries('pg_locks'))

    def test_changed_applied_migration_fails(self):
        connection=FakeConnection(applied={'a.sql':'old'})
        with self.assertRaisesRegex(RuntimeError,'Изменена уже применённая миграция'):
            self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()')])

    def test_busy_advisory_lock_stops_before_migrations(self):
        connection=FakeConnection(lock_acquired=False)
        with self.assertRaisesRegex(RuntimeError,'Другой процесс'):
            self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()')])
        self.assertFalse(connection.queries('CREATE TABLE one'))

    def test_no_transaction_file_runs_after_commit_one_statement_at_a_time(self):
        connection=FakeConnection(indexes={'leads_status_idx':[None,True],'leads_seen_idx':[None,True]})
        self.run_migrations(connection,[migration('a.sql','CREATE TABLE one()'),migration('idx.sql',NOTX),
                                        migration('c.sql','CREATE TABLE three()')])
        self.assertEqual(connection.commits,1)
        creates=connection.queries('CREATE INDEX CONCURRENTLY')
        self.assertEqual(len(creates),2)
        self.assertTrue(all(autocommit for _,_,autocommit in creates))
        self.assertNotIn(';',creates[0][0]+creates[1][0])
        self.assertTrue(creates[1][0].startswith('-- second one\nCREATE INDEX'))
        three=connection.calls.index(connection.queries('CREATE TABLE three')[0])
        self.assertGreater(connection.calls.index(creates[0]),three)
        self.assertFalse([call for call in connection.calls if not call[2] and 'INSERT' in call[0] and 'idx.sql' in str(call[1])])
        record=connection.queries('ON CONFLICT (name) DO NOTHING')
        self.assertEqual(record[0][1][0],'idx.sql')
        self.assertTrue(record[0][2])
        self.assertIn(("SELECT set_config('statement_timeout',%s,false)",('0',),True),connection.calls)
        self.assertIn(("SELECT set_config('lock_timeout',%s,false)",('60s',),True),connection.calls)
        self.assertFalse(connection.queries('DROP INDEX'))

    def test_applied_no_transaction_file_is_skipped(self):
        notx=migration('idx.sql',NOTX)
        connection=FakeConnection(applied={'idx.sql':notx.checksum})
        self.run_migrations(connection,[notx])
        self.assertFalse(connection.queries('CREATE INDEX'))
        self.assertFalse(connection.queries('set_config(\'statement_timeout\',%s,false)'))

    def test_invalid_index_is_dropped_and_rebuilt(self):
        connection=FakeConnection(indexes={'leads_status_idx':[False,True],'leads_seen_idx':[None,True]})
        self.run_migrations(connection,[migration('idx.sql',NOTX)])
        names=[query for query,_,_ in connection.calls if 'INDEX CONCURRENTLY' in query]
        self.assertEqual(names[0],'DROP INDEX CONCURRENTLY IF EXISTS leads_status_idx')
        self.assertIn('CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_status_idx',names[1])
        self.assertIn('невалиден',self.output)

    def test_index_still_invalid_after_build_fails_loudly(self):
        connection=FakeConnection(indexes={'leads_status_idx':[None,False]})
        with self.assertRaisesRegex(RuntimeError,'leads_status_idx'):
            self.run_migrations(connection,[migration('idx.sql',NOTX)])
        self.assertFalse(connection.queries('ON CONFLICT (name) DO NOTHING'))

    def test_concurrent_statement_lock_failure_retries_and_repairs_invalid_index(self):
        connection=FakeConnection(indexes={'leads_status_idx':[None,False,True],'leads_seen_idx':[None,True]})
        connection.fail('CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_status_idx',LockError('lock timeout'))
        sleep=self.run_migrations(connection,[migration('idx.sql',NOTX)])
        self.assertEqual(sleep.call_count,1)
        self.assertEqual(len(connection.queries('DROP INDEX CONCURRENTLY IF EXISTS leads_status_idx')),1)
        self.assertEqual(len(connection.queries('CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_status_idx')),2)
        self.assertEqual(len(connection.queries('ON CONFLICT (name) DO NOTHING')),1)


class WebMigrateParsing(unittest.TestCase):
    def test_no_transaction_marker_must_be_exact_first_line(self):
        self.assertTrue(web_migrate.is_no_transaction('-- leadgen:no-transaction\nSELECT 1;'))
        self.assertTrue(web_migrate.is_no_transaction('-- leadgen:no-transaction\r\nSELECT 1;'))
        self.assertFalse(web_migrate.is_no_transaction('SELECT 1;\n-- leadgen:no-transaction\n'))
        self.assertFalse(web_migrate.is_no_transaction('-- leadgen:no-transaction please\nSELECT 1;'))
        self.assertFalse(web_migrate.is_no_transaction(' -- leadgen:no-transaction\nSELECT 1;'))

    def test_split_statements_skips_comment_only_chunks(self):
        statements=web_migrate.split_statements(NOTX+'\nSELECT 3')
        self.assertEqual(len(statements),3)
        self.assertTrue(statements[0].startswith('-- leadgen:no-transaction'))
        self.assertTrue(statements[0].endswith('ON leadgen.leads(status)'))
        self.assertEqual(statements[2],'-- trailing comment only\n\nSELECT 3')
        self.assertEqual(web_migrate.split_statements('-- only\n;\n\n  ;\n'),[])
        self.assertEqual(web_migrate.split_statements("SELECT 'a;b';\r\nSELECT 2;  \n"),["SELECT 'a;b'",'SELECT 2'])

    def test_concurrent_index_name(self):
        name=web_migrate.concurrent_index_name
        self.assertEqual(name('-- c\nCREATE INDEX CONCURRENTLY IF NOT EXISTS leads_x ON leadgen.leads(a)'),'leads_x')
        self.assertEqual(name('create unique index concurrently leadgen.leads_y on leadgen.leads(a)'),'leadgen.leads_y')
        self.assertEqual(name('CREATE INDEX CONCURRENTLY "Leads Z" ON leads(a)'),'"Leads Z"')
        self.assertEqual(name('CREATE INDEX CONCURRENTLY IF NOT EXISTS leadgen . leads_q\n ON leads(a)'),'leadgen.leads_q')
        self.assertIsNone(name('CREATE INDEX leads_x ON leads(a)'))
        self.assertIsNone(name('ALTER TABLE leads ADD COLUMN x int'))

    def test_settings_from_env(self):
        self.assertEqual(Settings.from_env({}),Settings())
        settings=Settings.from_env({'LEADGEN_MIGRATE_LOCK_ATTEMPTS':'8','LEADGEN_MIGRATE_RETRY_BASE_SECONDS':'0.5',
                                    'LEADGEN_MIGRATE_LOCK_TIMEOUT':'5s','LEADGEN_MIGRATE_CONCURRENT_STATEMENT_TIMEOUT':'30min'})
        self.assertEqual((settings.attempts,settings.retry_base,settings.lock_timeout,settings.concurrent_statement_timeout),
                         (8,0.5,'5s','30min'))
        for key,value in (('LOCK_ATTEMPTS','0'),('LOCK_ATTEMPTS','x'),('RETRY_MAX_SECONDS','-1'),('LOCK_TIMEOUT',"1s'; DROP")):
            with self.assertRaises(ValueError):Settings.from_env({'LEADGEN_MIGRATE_'+key:value})

    def test_real_psycopg_lock_error_is_detected(self):
        try:import psycopg.errors
        except ImportError:self.skipTest('psycopg не установлен')
        self.assertTrue(web_migrate.is_lock_error(psycopg.errors.LockNotAvailable('lock timeout')))
        self.assertFalse(web_migrate.is_lock_error(psycopg.errors.QueryCanceled('statement timeout')))

    def test_registered_migrations_exist_and_index_file_is_no_transaction(self):
        migrations={item.name:item for item in web_migrate.load_migrations()}
        self.assertEqual(list(migrations),list(web_migrate.MIGRATIONS))
        self.assertEqual(len(set(web_migrate.MIGRATIONS)),len(web_migrate.MIGRATIONS))
        self.assertEqual(list(web_migrate.MIGRATIONS),sorted(web_migrate.MIGRATIONS))
        self.assertTrue(migrations['20261013150000_leads_query_indexes.sql'].no_transaction)
        fk_indexes=migrations['20261014130000_user_leads_fk_indexes.sql']
        self.assertTrue(fk_indexes.no_transaction)
        names=[web_migrate.concurrent_index_name(statement) for statement in web_migrate.split_statements(fk_indexes.sql)]
        self.assertEqual([name for name in names if name],['lead_assignments_owner_lead','telegram_replies_owner_lead'])
        self.assertFalse(migrations['20261014120000_filtered_cleanup_keeps_cursor.sql'].no_transaction)


if __name__=='__main__':
    unittest.main()
