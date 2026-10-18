"""Cursor-based lead delivery, delivery cadence, read transactions and worker-loop resilience."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import test_web_postgres as fixtures
from leadgen import product
from leadgen.database import Database, transient_errors
from leadgen.product import DeliveryGate, lead_id_floor, next_cursor

try:
    import psycopg
except ImportError:
    psycopg = None


class Cursor:
    def __init__(self,row=None,rows=()):self.row,self.rows=row,list(rows)
    def fetchone(self):return self.row
    def fetchall(self):return self.rows
    def __iter__(self):return iter(self.rows)


class FakeConnection:
    """psycopg-like connection: any statement opens a transaction, commit/rollback end it."""
    def __init__(self,answer=None):
        self.status='IDLE';self.statements=[];self.commits=0;self.rollbacks=0;self.closed=False;self.broken=False
        self.answer=answer or (lambda query,params:Cursor())

    @property
    def info(self):return SimpleNamespace(transaction_status=SimpleNamespace(name=self.status))

    def execute(self,query,params=()):
        self.statements.append(query);self.status='INTRANS'
        return self.answer(query,params)

    def commit(self):self.commits+=1;self.status='IDLE'
    def rollback(self):self.rollbacks+=1;self.status='IDLE'
    def close(self):self.closed=True


class CursorMath(unittest.TestCase):
    def test_advances_to_horizon_without_pending_leads(self):
        self.assertEqual(next_cursor(10,50,set()),50)

    def test_stops_below_oldest_pending_lead(self):
        # Lead 31 waits for its AI score or the plan delay: it must stay above the cursor.
        self.assertEqual(next_cursor(10,50,{31,45}),30)

    def test_never_passes_the_horizon(self):
        # Leads above the horizon were found less than the lag ago: a lower id may still be uncommitted.
        self.assertEqual(next_cursor(10,20,{40}),20)

    def test_never_moves_back(self):
        self.assertEqual(next_cursor(100,50,set()),100)
        self.assertEqual(next_cursor(100,500,{90}),100)


class WindowFloor(unittest.TestCase):
    def setUp(self):product._FLOORS.clear()
    def tearDown(self):product._FLOORS.clear()

    def test_floor_is_cached_and_bounded_scan(self):
        connection=FakeConnection(lambda query,params:Cursor({'id':77}))
        db=Database(connection,'postgres','dsn');now=[0.0]
        self.assertEqual(lead_id_floor(db,74,ttl=300,clock=lambda:now[0]),77)
        self.assertEqual(lead_id_floor(db,74,ttl=300,clock=lambda:now[0]),77)
        self.assertEqual(len(connection.statements),1)
        self.assertIn('ORDER BY id DESC LIMIT 1',connection.statements[0])
        now[0]=301
        lead_id_floor(db,74,ttl=300,clock=lambda:now[0])
        self.assertEqual(len(connection.statements),2)

    def test_empty_table_has_zero_floor(self):
        db=Database(FakeConnection(lambda query,params:Cursor(None)),'postgres','dsn')
        self.assertEqual(lead_id_floor(db,10),0)

    def test_window_covers_freshness_limit(self):
        self.assertEqual(product.window_hours({'max_age_hours':72}),74)
        self.assertEqual(product.window_hours({}),74)


class Cadence(unittest.TestCase):
    def make(self,**kwargs):
        self.now=[0.0];self.max_id=[10]
        connection=FakeConnection(lambda query,params:Cursor({'id':self.max_id[0]}))
        self.db=Database(connection,'postgres','dsn');self.connection=connection
        return DeliveryGate(clock=lambda:self.now[0],**kwargs)

    def test_runs_only_on_new_leads_backlog_force_or_idle(self):
        gate=self.make(min_seconds=2,idle_seconds=15)
        self.assertTrue(gate.due(self.db));gate.ran(0)
        self.now[0]=1;self.assertFalse(gate.due(self.db))          # minimum spacing
        self.now[0]=3;self.assertFalse(gate.due(self.db))          # nothing new
        self.max_id[0]=11;self.assertTrue(gate.due(self.db));gate.ran(0)  # a new lead arrived
        self.now[0]=6;self.assertFalse(gate.due(self.db))
        self.now[0]=19;self.assertTrue(gate.due(self.db));gate.ran(1)     # idle interval: delays and AI waits expire
        self.now[0]=21;self.assertTrue(gate.due(self.db));gate.ran(0)     # backlog: one card per user per pass
        self.now[0]=21.5;self.assertTrue(gate.due(self.db,force=True))    # a source scan just finished
        self.assertEqual(self.connection.status,'IDLE')                   # the max(id) probe leaves no transaction

    def test_new_leads_during_a_pass_trigger_the_next_one(self):
        gate=self.make(min_seconds=0,idle_seconds=60)
        self.assertTrue(gate.due(self.db))
        self.max_id[0]=12  # inserted while the pass was running
        gate.ran(0);self.now[0]=1
        self.assertTrue(gate.due(self.db))

    def test_environment_configures_intervals(self):
        with patch.dict(os.environ,{'LEAD_DELIVERY_MIN_SECONDS':'5','LEAD_DELIVERY_IDLE_SECONDS':'oops'}):
            gate=DeliveryGate()
        self.assertEqual((gate.min_seconds,gate.idle_seconds),(5,30))


class ReadTransactions(unittest.TestCase):
    def test_release_commits_an_open_read_transaction(self):
        connection=FakeConnection();db=Database(connection,'postgres','dsn')
        db.execute('SELECT 1')
        self.assertEqual(connection.status,'INTRANS')
        db.release()
        self.assertEqual((connection.status,connection.commits),('IDLE',1))
        db.release()  # nothing open: no extra round trip
        self.assertEqual(connection.commits,1)

    def test_release_keeps_multi_statement_writes_atomic(self):
        connection=FakeConnection();db=Database(connection,'postgres','dsn')
        with db:
            db.execute('INSERT 1');db.release();db.execute('INSERT 2')
            self.assertEqual(connection.commits,0)
        self.assertEqual(connection.commits,1)
        with self.assertRaises(ValueError):
            with db:
                db.execute('INSERT 3');db.release()
                raise ValueError('boom')
        self.assertEqual((connection.commits,connection.rollbacks),(1,1))
        db.execute('SELECT 1');db.release()
        self.assertEqual(connection.status,'IDLE')  # depth is back to zero after the failed block

    def test_release_rolls_back_a_failed_transaction(self):
        connection=FakeConnection();db=Database(connection,'postgres','dsn');connection.status='INERROR'
        db.release()
        self.assertEqual((connection.rollbacks,connection.commits),(1,0))

    def test_sqlite_is_untouched(self):
        connection=MagicMock();Database(connection,'sqlite','x').release()
        connection.commit.assert_not_called()

    def test_recover_reopens_a_broken_connection(self):
        old=FakeConnection();old.broken=True;db=Database(old,'postgres','dsn');new=FakeConnection()
        with patch('leadgen.database.db_open',return_value=SimpleNamespace(connection=new)) as reopen:
            db.recover()
        reopen.assert_called_once_with('dsn')
        self.assertTrue(old.closed);self.assertIs(db.connection,new)

    def test_recover_rolls_back_a_healthy_connection(self):
        connection=FakeConnection();connection.status='INERROR';db=Database(connection,'postgres','dsn')
        with patch('leadgen.database.db_open') as reopen:db.recover()
        reopen.assert_not_called();self.assertEqual(connection.rollbacks,1)

    @unittest.skipUnless(psycopg,'psycopg is not installed')
    def test_transient_errors_exclude_programming_errors(self):
        errors=transient_errors()
        self.assertTrue(issubclass(psycopg.errors.QueryCanceled,errors))
        self.assertTrue(issubclass(psycopg.InterfaceError,errors))
        self.assertFalse(issubclass(psycopg.errors.UndefinedColumn,errors))
        self.assertFalse(issubclass(psycopg.errors.UniqueViolation,errors))


class StopLoop(Exception):
    pass


@unittest.skipUnless(psycopg,'psycopg is not installed')
class WorkerLoop(unittest.TestCase):
    """run_bot survives statement timeouts and lost connections and never waits on Telegram inside a transaction."""
    def run_loop(self,deliveries,iterations=3,connection=None,extra_env=None):
        from leadgen import bot
        self.connection=connection or FakeConnection(self.answer);db=Database(self.connection,'postgres','dsn')
        self.max_id=0;self.polls=[];self.sleeps=[];self.calls=0
        def telegram(method,payload):
            if method=='getWebhookInfo':return {}
            self.polls.append(db.connection.info.transaction_status.name)
            if len(self.polls)>iterations:raise StopLoop()
            return []
        def deliver(*args,**kwargs):
            self.calls+=1;outcome=deliveries[min(self.calls,len(deliveries))-1]
            if isinstance(outcome,BaseException):raise outcome
            return outcome
        env={'TELEGRAM_CHAT_ID':'1','LEAD_DELIVERY_MIN_SECONDS':'0',**(extra_env or {})}
        with patch.dict(os.environ,env),patch('leadgen.bot.telegram',telegram),patch('leadgen.bot.env_load'),\
                patch('leadgen.bot.capture_exception') as captured,\
                patch('leadgen.bot.time.sleep',side_effect=self.sleeps.append),\
                patch('leadgen.telegram_monitor.reader_configured',return_value=False),\
                patch('leadgen.telegram_monitor.ReaderThread') as threads,patch('leadgen.bot.beat') as beat,\
                patch('leadgen.ai_scoring.scoring_enabled',return_value=False),\
                patch('leadgen.integrations.integrations_enabled',return_value=False),\
                patch('leadgen.reminders.deliver_due'),patch('leadgen.team_notifications.deliver_assignments'),\
                patch('leadgen.product.notify_subscriptions'),patch('leadgen.product.backfill_pending'),\
                patch('leadgen.feedback.send_surveys'),patch('leadgen.feedback.deliver_notifications'),\
                patch('leadgen.product.deliver_registered',side_effect=deliver):
            with self.assertRaises(StopLoop):
                bot.run_bot(db,{'poll_seconds':300,'sources':[]})
        self.captured=captured;self.beats=beat.call_count;self.threads=threads
        return db

    def answer(self,query,params):
        if 'max(id)' in query:
            self.max_id+=1;return Cursor({'id':self.max_id})
        if 'monitoring_active=true' in query:return Cursor({'?column?':1})
        return Cursor()

    def test_query_canceled_rolls_back_backs_off_and_continues(self):
        canceled=psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
        self.run_loop([canceled,0,0])
        self.assertEqual(self.calls,3)              # delivery kept running after the timeout
        self.assertEqual(self.sleeps,[2])           # one short back-off
        self.assertGreaterEqual(self.connection.rollbacks,1)
        self.captured.assert_called_once()
        self.assertEqual(self.beats,2)              # heartbeat only after the two passes that completed
        # Every long-poll starts outside a transaction: no ACCESS SHARE locks held while waiting for Telegram.
        self.assertEqual(set(self.polls),{'IDLE'})

    def test_personal_monitors_thread_is_supervised_by_the_loop(self):
        env={'TELEGRAM_API_ID':'1','TELEGRAM_API_HASH':'h','TELEGRAM_SESSION_ENCRYPTION_KEY':'k'}
        self.run_loop([0,0,0],extra_env=env)
        names=[c.kwargs.get('name') for c in self.threads.call_args_list]
        self.assertIn('personal-monitors',names)
        # Started once, then re-checked on every pass: a dead manager thread is restarted (rate-limited).
        self.assertGreaterEqual(self.threads.return_value.ensure.call_count,4)

    def test_lost_connection_is_reopened(self):
        lost=psycopg.OperationalError('server closed the connection unexpectedly')
        connection=FakeConnection(self.answer);fresh=FakeConnection(self.answer)
        connection.broken=True  # what psycopg reports after the server dropped the session
        with patch('leadgen.database.db_open',return_value=SimpleNamespace(connection=fresh)) as reopen:
            db=self.run_loop([lost,0],connection=connection)
        reopen.assert_called_once_with('dsn')
        self.assertIs(db.connection,fresh)

    def test_programming_errors_still_stop_the_worker(self):
        from leadgen import bot
        with self.assertRaises(psycopg.errors.UndefinedColumn):
            self.run_loop([psycopg.errors.UndefinedColumn('column "x" does not exist')])
        self.assertEqual(self.sleeps,[])
        self.assertTrue(bot.db_retry_delay(1)==2 and bot.db_retry_delay(10)==bot.DB_RETRY_MAX_SECONDS)


CONFIG={'min_budget':0,'max_age_hours':72,'sources':[]}


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class CursorPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        product._FLOORS.clear()
        with self.db:
            self.db.execute('DELETE FROM leads')  # the shared fixture's sample leads
            self.db.execute("UPDATE app_users SET registered_at=now()-interval '1 day'")
            self.db.execute('UPDATE user_preferences SET monitoring_active=(user_id=?),show_without_budget=true',(self.u,))
        self.sent=[];self.send=lambda method,data:self.sent.append(data)
        self.env=patch.dict(os.environ,{'LEAD_CURSOR_LAG_SECONDS':'600'});self.env.start()

    def tearDown(self):self.env.stop();product._FLOORS.clear()

    def add(self,n,minutes):
        now=datetime.now(timezone.utc)
        payload=dict(source='Telegram: test',url=f'https://t.me/business_chat/c{n}',title=f'Нужен сайт для клиники {n}',
                     text=f'Нужен сайт для клиники {n} с онлайн-записью, бюджет 80 000 рублей',
                     published=(now-timedelta(minutes=minutes)).isoformat(),budget_text='80 000 рублей')
        with self.db:
            return self.db.execute('INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?) RETURNING id',
                (payload['url'],f'fp{n}',json.dumps(payload,ensure_ascii=False),'ready','test',
                 (now-timedelta(minutes=minutes)).isoformat())).fetchone()['id']

    def cursor(self):
        return self.db.execute('SELECT delivery_cursor FROM user_preferences WHERE user_id=?',(self.u,)).fetchone()['delivery_cursor']

    def deliver(self,limit=10):return product.deliver_registered(self.db,CONFIG,self.send,limit_per_user=limit)

    def test_cursor_advances_and_nothing_is_sent_twice(self):
        old=[self.add(i,60) for i in range(3)]
        self.assertEqual(self.deliver(),3)
        self.assertEqual(self.cursor(),max(old))                   # all settled and older than the lag
        self.assertEqual(self.deliver(),0)                         # repeated passes never resend
        fresh=self.add(10,1)
        self.assertEqual(self.deliver(),1)
        self.assertEqual(self.cursor(),max(old))                   # a fresh lead is not passed yet
        self.assertEqual(self.deliver(),0)
        # The transaction is closed after the pass: nothing holds locks on leads between iterations.
        self.assertEqual(self.db.connection.info.transaction_status.name,'IDLE')
        self.assertEqual(len(self.sent),4)
        self.assertEqual(len({card['text'] for card in self.sent}),4)
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM user_leads WHERE user_id=? AND delivery_status='sent'",
                                         (self.u,)).fetchone()['n'],4)
        self.assertIsNone(self.db.execute('SELECT 1 FROM user_leads WHERE lead_id=? AND user_id=?',(fresh,self.other)).fetchone())

    def test_pending_lead_holds_the_cursor_until_delivered(self):
        first=self.add(1,60);second=self.add(2,50);third=self.add(3,40)
        self.assertEqual(self.deliver(limit=1),1)                  # newest first, one card per pass
        self.assertEqual(self.cursor(),first-1)                    # the two unsent leads stay above the cursor
        self.assertEqual(self.deliver(limit=1),1)
        self.assertEqual(self.cursor(),first-1)
        self.assertEqual(self.deliver(limit=1),1)
        self.assertEqual(self.cursor(),third)
        self.assertIn('клиники 3',self.sent[0]['text'])
        self.assertEqual(self.deliver(limit=1),0)
        self.assertEqual(len(self.sent),3)
        self.assertLess(first,second)
        self.assertLess(second,third)

    def test_late_commit_below_max_id_is_not_skipped(self):
        # Ids are allocated at INSERT and visible at COMMIT: a lead with a lower id can appear after a higher one.
        late=self.add(1,0.5)
        with self.db:self.db.execute('DELETE FROM leads WHERE id=?',(late,))
        newer=self.add(2,0.2)
        self.assertEqual(self.deliver(),1)
        self.assertLess(self.cursor(),newer)                       # the horizon (10 min ago) keeps the cursor back
        # Re-insert the "late" lead with its lower id, as a slow concurrent transaction would commit it.
        now=datetime.now(timezone.utc)
        payload=dict(source='Telegram: test',url='https://t.me/business_chat/late',title='Нужен сайт для клиники late',
                     text='Нужен сайт для клиники с онлайн-записью, бюджет 80 000 рублей',published=now.isoformat(),
                     budget_text='80 000 рублей')
        with self.db:self.db.execute('''INSERT INTO leads(id,url,fingerprint,payload,status,reason,first_seen)
            OVERRIDING SYSTEM VALUE VALUES(?,?,?,?,?,?,?)''',(late,payload['url'],'fp-late',json.dumps(payload,ensure_ascii=False),
            'ready','test',now.isoformat()))
        self.assertEqual(self.deliver(),1)
        self.assertIn('late',self.sent[-1]['text'])

    def test_filter_change_resets_the_cursor(self):
        lead=self.add(1,60)
        with self.db:self.db.execute("UPDATE user_preferences SET topics='[\"Боты\"]'::jsonb WHERE user_id=?",(self.u,))
        self.assertEqual(self.deliver(),0)                         # filtered under the narrow rules
        self.assertEqual(self.cursor(),lead)
        with self.db:
            self.db.execute("UPDATE user_preferences SET topics=?::jsonb,updated_at=now() WHERE user_id=?",
                            (json.dumps(list(product.TOPICS),ensure_ascii=False),self.u))
            self.db.execute("DELETE FROM user_leads WHERE user_id=? AND delivery_status='filtered'",(self.u,))
        self.assertEqual(self.cursor(),0)                          # trigger: the lead is evaluated again
        self.assertEqual(self.deliver(),1)

    def test_stale_pass_does_not_move_a_reset_cursor(self):
        self.add(1,60)
        prefs=dict(self.db.execute(product.RECIPIENTS+' AND u.id=?',(self.u,)).fetchone())
        self.db.release()
        with self.db:self.db.execute('UPDATE user_preferences SET updated_at=now()+interval \'1 second\' WHERE user_id=?',(self.u,))
        product._deliver_user(self.db,CONFIG,self.send,prefs,10,(False,0),0,10**12)
        self.assertEqual(self.cursor(),0)
