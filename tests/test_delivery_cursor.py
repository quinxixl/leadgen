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


def worker_patches(stack,deliver,sleeps,extra=()):
    """The DeliveryWorker's collaborators, faked: only deliver_registered does something."""
    for target,kwargs in [('leadgen.bot.capture_exception',{}),('leadgen.bot.time.sleep',{'side_effect':sleeps.append}),
            ('leadgen.ai_scoring.scoring_enabled',{'return_value':False}),
            ('leadgen.integrations.integrations_enabled',{'return_value':False}),
            ('leadgen.reminders.deliver_due',{'return_value':0}),
            ('leadgen.team_notifications.deliver_assignments',{'return_value':0}),
            ('leadgen.product.notify_subscriptions',{}),('leadgen.product.backfill_pending',{}),
            ('leadgen.feedback.send_surveys',{}),('leadgen.feedback.deliver_notifications',{}),
            ('leadgen.product.deliver_registered',{'side_effect':deliver}),*extra]:
        yield target,stack.enter_context(patch(target,**kwargs))


@unittest.skipUnless(psycopg,'psycopg is not installed')
class WorkerLoop(unittest.TestCase):
    """DeliveryWorker survives statement timeouts and lost connections and never waits on Telegram inside a transaction."""
    def run_worker(self,deliveries,connection=None,extra_env=None):
        from contextlib import ExitStack
        from leadgen import bot
        self.connection=connection or FakeConnection(self.answer);db=Database(self.connection,'postgres','dsn')
        self.max_id=0;self.sleeps=[];self.calls=0;self.states=[]
        worker=bot.DeliveryWorker(lambda:db,{'poll_seconds':300,'sources':[]},lambda method,payload:{},bot.FirstLeads(),tick=0)
        def deliver(*args,**kwargs):
            self.states.append(db.connection.info.transaction_status.name)
            self.calls+=1;outcome=deliveries[min(self.calls,len(deliveries))-1]
            if self.calls>=len(deliveries):worker.stop()
            if isinstance(outcome,BaseException):raise outcome
            return outcome
        env={'LEAD_DELIVERY_MIN_SECONDS':'0',**(extra_env or {})}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,env))
            mocks=dict(worker_patches(stack,deliver,self.sleeps))
            worker.gate=product.DeliveryGate()  # reads LEAD_DELIVERY_MIN_SECONDS
            self.worker=worker;self.captured=mocks['leadgen.bot.capture_exception']
            worker.run()
        return db

    def answer(self,query,params):
        if 'max(id)' in query:
            self.max_id+=1;return Cursor({'id':self.max_id})
        if 'monitoring_active=true' in query:return Cursor({'?column?':1})
        return Cursor()

    def test_query_canceled_rolls_back_backs_off_and_continues(self):
        canceled=psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
        self.run_worker([canceled,0,0])
        self.assertEqual(self.calls,3)              # delivery kept running after the timeout
        self.assertEqual(self.sleeps,[2])           # one short back-off
        self.assertGreaterEqual(self.connection.rollbacks,1)
        self.captured.assert_called_once()
        # Every delivery pass starts outside a transaction: no ACCESS SHARE locks held while it talks to Telegram.
        self.assertEqual(set(self.states),{'IDLE'})
        self.assertTrue(self.connection.closed)     # the worker's own connection is closed when it stops

    def test_heartbeat_waits_for_a_completed_worker_pass(self):
        from leadgen import bot
        self.run_worker([0])
        self.assertTrue(self.worker.healthy())
        self.worker.last_pass-=bot.WORKER_STALE_SECONDS+1  # failing on every pass since then
        self.assertFalse(self.worker.healthy())

    def test_lost_connection_is_reopened(self):
        lost=psycopg.OperationalError('server closed the connection unexpectedly')
        connection=FakeConnection(self.answer);fresh=FakeConnection(self.answer)
        connection.broken=True  # what psycopg reports after the server dropped the session
        with patch('leadgen.database.db_open',return_value=SimpleNamespace(connection=fresh)) as reopen:
            db=self.run_worker([lost,0],connection=connection)
        reopen.assert_called_once_with('dsn')
        self.assertIs(db.connection,fresh)

    def test_programming_errors_are_kept_for_the_update_loop(self):
        from leadgen import bot
        with self.assertRaises(psycopg.errors.UndefinedColumn):
            self.run_worker([psycopg.errors.UndefinedColumn('column "x" does not exist')])
        self.assertIsInstance(self.worker.fatal,psycopg.errors.UndefinedColumn)
        self.assertEqual(self.sleeps,[])
        self.assertTrue(bot.db_retry_delay(1)==2 and bot.db_retry_delay(10)==bot.DB_RETRY_MAX_SECONDS)

    def test_pending_welcome_batch_runs_first_and_its_user_is_skipped_meanwhile(self):
        from contextlib import ExitStack
        from leadgen import bot
        db=Database(FakeConnection(self.answer),'postgres','dsn');self.max_id=0
        first=bot.FirstLeads();order=[]
        worker=bot.DeliveryWorker(lambda:db,{'poll_seconds':300,'sources':[]},lambda method,payload:{},first,tick=0)
        first.reserve(7)  # a handler is switching monitoring on for user 7
        def deliver(*args,**kwargs):order.append(('live',kwargs['skip']()));return 0
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'LEAD_DELIVERY_MIN_SECONDS':'0'}))
            mocks=dict(worker_patches(stack,deliver,[],[('leadgen.product.backfill_first_leads',
                {'side_effect':lambda db,base,send,user_id:order.append(('welcome',user_id))})]))
            worker.gate=product.DeliveryGate();worker._connect()
            worker.run_once()
            first.release(7)  # its confirmation messages are out
            worker.run_once();worker.run_once()
        # Every pass delivers (new max(id) each time); the welcome batch runs once, before user 7's first live pass.
        self.assertEqual(order,[('live',frozenset({7})),('welcome',7),('live',frozenset()),('live',frozenset())])
        self.assertEqual(first.held(),frozenset())
        # The site's first-run batches skip held users too: it must not run between the handler's commit and its reply.
        self.assertEqual(mocks['leadgen.product.backfill_pending'].call_args.kwargs['skip'],first.held)

    def test_unreachable_database_at_start_is_retried_without_alerts(self):
        from contextlib import ExitStack
        from leadgen import bot
        db=Database(FakeConnection(self.answer),'postgres','dsn');self.max_id=0;attempts=[]
        def open_db():
            attempts.append(1)
            if len(attempts)==1:raise RuntimeError('Supabase Postgres: не удалось подключиться после 3 попыток')
            return db
        worker=bot.DeliveryWorker(open_db,{'poll_seconds':300,'sources':[]},lambda method,payload:{},bot.FirstLeads(),tick=0)
        sleeps=[]
        def deliver(*args,**kwargs):
            worker.stop();return 0
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'LEAD_DELIVERY_MIN_SECONDS':'0'}))
            mocks=dict(worker_patches(stack,deliver,sleeps))
            worker.run()
        self.assertEqual((len(attempts),sleeps),(2,[3]))
        mocks['leadgen.bot.capture_exception'].assert_not_called()

    def test_held_users_are_read_after_the_recipients(self):
        # A handler reserves before it commits monitoring_active=true: reading held() after the recipients means a
        # recipient list that already contains the user always comes with its reservation.
        events=[]
        def answer(query,params):
            if 'monitoring_active=true' in query:
                events.append('recipients');return Cursor(rows=[{'id':7}])
            return Cursor()
        db=Database(FakeConnection(answer),'postgres','dsn')
        def held():
            events.append('held');return frozenset({7})
        with patch('leadgen.ai_scoring.scoring_enabled',return_value=False),\
                patch('leadgen.product._deliver_user',side_effect=AssertionError('held user delivered')),\
                patch('leadgen.product.backfill_first_leads',side_effect=AssertionError('held user backfilled')):
            self.assertEqual(product.deliver_registered(db,{'max_age_hours':72},print,skip=held),0)
            self.assertEqual(product.backfill_pending(db,{},print,skip=held),0)
        self.assertEqual(events,['recipients','held','recipients','held'])


@unittest.skipUnless(psycopg,'psycopg is not installed')
class UpdateLoop(unittest.TestCase):
    """run_bot only long-polls and handles updates; delivery runs on the worker thread with its own connection."""
    def run_bot(self,poll,deliver_queues=None,iterations=2):
        """poll(n) answers the n-th getUpdates (0-based); the loop is stopped after `iterations` polls."""
        from contextlib import ExitStack
        import threading
        from leadgen import bot
        answer=lambda query,params:Cursor({'id':1}) if 'max(id)' in query else Cursor()
        db=Database(FakeConnection(answer),'postgres','dsn');self.polls=[];self.handled=[]
        self.worker_connection=FakeConnection(answer)
        def telegram(method,payload):
            if method!='getUpdates':return {}
            self.polls.append(payload)
            if len(self.polls)>iterations:raise StopLoop()
            return poll(len(self.polls)-1)
        with ExitStack() as stack:
            stack.enter_context(patch('leadgen.bot.telegram',telegram))
            stack.enter_context(patch('leadgen.bot.env_load'));stack.enter_context(patch('leadgen.bot.beat'))
            stack.enter_context(patch('leadgen.telegram_monitor.reader_configured',return_value=False))
            stack.enter_context(patch('leadgen.database.db_open',
                                      return_value=Database(self.worker_connection,'postgres','dsn')))
            stack.enter_context(patch('leadgen.product.ProductController.handle',
                lambda ui,update:self.handled.append((update['update_id'],threading.current_thread().name))))
            dict(worker_patches(stack,lambda *a,**k:0,[],
                [('leadgen.bot.deliver_queues',{'side_effect':deliver_queues or (lambda db,send:False)})]))
            with self.assertRaises(StopLoop):
                bot.run_bot(db,{'poll_seconds':300,'sources':[]})

    def test_long_poll_timeout_is_shorter_than_the_http_timeout(self):
        from leadgen import bot
        from leadgen.app import bot_api_timeout
        self.run_bot(lambda n:[])
        self.assertEqual({payload['timeout'] for payload in self.polls},{bot.LONG_POLL_SECONDS})
        self.assertTrue(20<=bot.LONG_POLL_SECONDS<=25)
        self.assertGreater(bot_api_timeout('getUpdates',self.polls[0]),bot.LONG_POLL_SECONDS)

    def test_updates_are_handled_while_the_worker_waits_for_a_slow_webhook(self):
        import threading
        stuck=threading.Event();release=threading.Event()
        def slow_webhook(db,send):
            stuck.set();release.wait(10)  # an external endpoint that hangs
            return False
        def poll(n):
            if n==0:
                stuck.wait(5)  # the worker is inside the webhook call now
                return [{'update_id':5}]
            return []
        try:self.run_bot(poll,slow_webhook)
        finally:release.set()
        self.assertTrue(stuck.is_set())
        self.assertEqual(self.handled,[(5,'MainThread')])  # handled at once, on the update loop
        self.assertTrue(self.worker_connection.statements)  # the worker queried on its own connection

    def test_programming_errors_in_the_worker_still_stop_the_bot(self):
        import time as clock
        def broken(db,send):raise psycopg.errors.UndefinedColumn('column "x" does not exist')
        def poll(n):
            clock.sleep(0.1)  # a long poll: the worker fails meanwhile
            return []
        with self.assertRaises(psycopg.errors.UndefinedColumn):
            self.run_bot(poll,broken,iterations=20)


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
