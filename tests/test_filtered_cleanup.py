"""Age-based cleanup of 'filtered' user_leads rows: batches, cadence, delivery cursor and FK indexes."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import filtered_cleanup, product, web_migrate
from leadgen.database import Database
from leadgen.filtered_cleanup import FilteredCleanup, cleanup_filtered, retention_hours
from test_delivery_cursor import Cursor, FakeConnection

try:
    import psycopg
except ImportError:
    psycopg = None

CONFIG={'min_budget':0,'max_age_hours':72,'sources':[]}


class DeleteCursor(Cursor):
    def __init__(self,rowcount):super().__init__();self.rowcount=rowcount


class Batches(unittest.TestCase):
    def setUp(self):product._FLOORS.clear()
    def tearDown(self):product._FLOORS.clear()

    def make(self,counts,floor=500):
        self.counts=list(counts)
        def answer(query,params):
            if 'ORDER BY id DESC LIMIT 1' in query:return Cursor({'id':floor} if floor else None)
            if query.lstrip().startswith('DELETE'):return DeleteCursor(self.counts.pop(0))
            return Cursor()
        self.connection=FakeConnection(answer)
        return Database(self.connection,'postgres','dsn')

    def deletes(self):return [q for q in self.connection.statements if q.lstrip().startswith('DELETE')]

    def test_retention_is_window_plus_margin(self):
        self.assertEqual(retention_hours({'max_age_hours':72}),74+filtered_cleanup.RETENTION_MARGIN_HOURS)

    def test_batches_until_a_short_one_each_in_its_own_transaction(self):
        db=self.make([3,3,1])
        self.assertEqual(cleanup_filtered(db,CONFIG,batch_size=3,max_batches=10),7)
        self.assertEqual(len(self.deletes()),3)
        self.assertEqual(self.connection.commits,4)                 # floor read released + one commit per batch
        self.assertEqual(self.connection.status,'IDLE')
        delete=self.deletes()[0]
        self.assertIn("delivery_status='filtered'",delete)
        self.assertIn('lead_id<=%s',delete)
        self.assertIn('LIMIT %s',delete)
        # Every batch marks its transaction so the cursor trigger keeps the users' cursors.
        marks=[q for q in self.connection.statements if 'set_config' in q]
        self.assertEqual(len(marks),3)

    def test_run_is_bounded_by_max_batches(self):
        db=self.make([2]*10)
        self.assertEqual(cleanup_filtered(db,CONFIG,batch_size=2,max_batches=4),8)
        self.assertEqual(len(self.deletes()),4)

    def test_nothing_old_enough_deletes_nothing(self):
        db=self.make([],floor=None)
        self.assertEqual(cleanup_filtered(db,CONFIG),0)
        self.assertEqual(self.deletes(),[])

    def test_sqlite_is_untouched(self):
        connection=FakeConnection()
        self.assertEqual(cleanup_filtered(Database(connection,'sqlite','x'),CONFIG),0)
        self.assertEqual(connection.statements,[])


class Cadence(unittest.TestCase):
    def test_runs_once_per_interval(self):
        now=[0.0];timer=FilteredCleanup(interval_seconds=3600,clock=lambda:now[0])
        self.assertTrue(timer.due())                               # first iteration after start
        for t in (1,60,3599):
            now[0]=t;self.assertFalse(timer.due())                 # not on every ~1 s loop iteration
        now[0]=3600;self.assertTrue(timer.due())
        now[0]=3601;self.assertFalse(timer.due())

    def test_run_if_due_skips_work_between_runs(self):
        now=[0.0];timer=FilteredCleanup(interval_seconds=3600,clock=lambda:now[0])
        with patch('leadgen.filtered_cleanup.cleanup_filtered',return_value=5) as cleanup,\
                patch('leadgen.filtered_cleanup.cleanup_rejected',return_value=7) as rejected,\
                patch('leadgen.filtered_cleanup.save_stats') as saved:
            self.assertEqual(timer.run_if_due('db',CONFIG),{'filtered':5,'leads':7})
            now[0]=10;self.assertIsNone(timer.run_if_due('db',CONFIG))
        cleanup.assert_called_once_with('db',CONFIG);rejected.assert_called_once_with('db',CONFIG)
        stats=saved.call_args.args[1]
        self.assertEqual((stats['filtered'],stats['leads'],stats['rejected_retention_hours']),(5,7,98))

    def test_rejected_retention_never_enters_the_freshness_window(self):
        for days,hours in (('1',retention_hours(CONFIG)),('3',98),('',98),('10',240),('0',0),('oops',98)):
            with patch.dict(os.environ,{'LEAD_REJECTED_RETENTION_DAYS':days}):
                self.assertEqual(filtered_cleanup.rejected_retention_hours(CONFIG),hours,days)

    def test_failed_run_waits_for_the_next_interval(self):
        now=[0.0];timer=FilteredCleanup(interval_seconds=3600,clock=lambda:now[0])
        with patch('leadgen.filtered_cleanup.cleanup_filtered',side_effect=ValueError('boom')) as cleanup:
            with self.assertRaises(ValueError):timer.run_if_due('db',CONFIG)
            now[0]=5;self.assertIsNone(timer.run_if_due('db',CONFIG))
        self.assertEqual(cleanup.call_count,1)

    def test_environment_configures_interval(self):
        with patch.dict(os.environ,{'FILTERED_CLEANUP_INTERVAL_SECONDS':'120'}):
            self.assertEqual(FilteredCleanup().interval,120)
        with patch.dict(os.environ,{'FILTERED_CLEANUP_INTERVAL_SECONDS':'1'}):
            self.assertEqual(FilteredCleanup().interval,60)
        with patch.dict(os.environ,{'FILTERED_CLEANUP_INTERVAL_SECONDS':'oops'}):
            self.assertEqual(FilteredCleanup().interval,3600)


@unittest.skipUnless(psycopg,'psycopg is not installed')
class WorkerLoop(unittest.TestCase):
    def run_loop(self,cleanup_effect,iterations=4):
        from contextlib import ExitStack
        from leadgen import bot
        from test_delivery_cursor import worker_patches
        def answer(query,params):
            if 'max(id)' in query:return Cursor({'id':1})
            if 'monitoring_active=true' in query:return Cursor({'?column?':1})
            return Cursor()
        db=Database(FakeConnection(answer),'postgres','dsn')
        worker=bot.DeliveryWorker(lambda:db,{'poll_seconds':300,'sources':[],'max_age_hours':72},
                                  lambda method,payload:{},bot.FirstLeads(),tick=0)
        passes=[]
        def deliver(*args,**kwargs):
            passes.append(1)
            if len(passes)>=iterations:worker.stop()
            return 0
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'LEAD_DELIVERY_MIN_SECONDS':'0'}))
            mocks=dict(worker_patches(stack,deliver,[]))
            cleanup=stack.enter_context(patch('leadgen.filtered_cleanup.cleanup_filtered',side_effect=cleanup_effect))
            worker.gate=product.DeliveryGate();worker.gate.idle_seconds=0  # one delivery pass per worker pass
            worker.run()
        return cleanup,mocks['leadgen.bot.capture_exception'],len(passes)

    def test_cleanup_runs_once_not_per_iteration(self):
        cleanup,captured,polls=self.run_loop(lambda db,base:0)
        self.assertEqual(polls,4)                                  # four full passes ran
        self.assertEqual(cleanup.call_count,1)
        captured.assert_not_called()

    def test_cleanup_failure_does_not_stop_the_worker(self):
        cleanup,captured,polls=self.run_loop(ValueError('boom'))
        self.assertEqual(polls,4)
        self.assertEqual(cleanup.call_count,1)                     # not retried on the next iteration
        self.assertEqual(captured.call_args.kwargs['component'],'filtered_cleanup')


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class CleanupPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.WebPostgres.setUpClass.__func__(cls)
        # The no-transaction index file, statement by statement, as web_migrate runs it.
        for name in ('20261014130000_user_leads_fk_indexes.sql','20261019120000_project_leads_lead_index.sql'):
            sql=(Path('supabase/migrations')/name).read_text()
            with psycopg.connect(fixtures.DSN,autocommit=True) as connection:
                for statement in web_migrate.split_statements(sql):connection.execute(statement)

    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        product._FLOORS.clear()
        with self.db:
            self.db.execute('DELETE FROM leads')
            self.db.execute("UPDATE app_users SET registered_at=now()-interval '30 days'")
            self.db.execute('UPDATE user_preferences SET monitoring_active=(user_id=?),show_without_budget=true',(self.u,))

    def tearDown(self):product._FLOORS.clear()

    def add(self,n,hours):
        seen=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        payload=dict(source='Telegram: test',url=f'https://t.me/business_chat/f{n}',title=f'Нужен сайт {n}',
                     text=f'Нужен сайт для клиники {n}, бюджет 80 000 рублей',published=seen,budget_text='80 000 рублей')
        with self.db:
            return self.db.execute('INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?) RETURNING id',
                (payload['url'],f'f{n}',json.dumps(payload,ensure_ascii=False),'ready','test',seen)).fetchone()['id']

    def mark(self,user_id,lead_id,status):
        with self.db:self.db.execute('INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,?)',(user_id,lead_id,status))

    def rows(self):
        return {(r['user_id'],r['lead_id'],r['delivery_status']) for r in
                self.db.execute('SELECT user_id,lead_id,delivery_status FROM user_leads ORDER BY 1,2').fetchall()}

    def cursors(self):
        return {r['user_id']:(r['delivery_cursor'],r['updated_at']) for r in
                self.db.execute('SELECT user_id,delivery_cursor,updated_at FROM user_preferences').fetchall()}

    def test_deletes_only_old_filtered_rows_in_batches_and_keeps_cursors(self):
        old=[self.add(i,150) for i in range(5)]                    # older than window (74 h) + margin (24 h)
        sent_old=self.add(12,150)                                  # ids grow with first_seen, as in production
        recent=self.add(10,80)                                     # outside the window, inside the margin
        fresh=self.add(11,1)
        for lead in old:
            self.mark(self.u,lead,'filtered');self.mark(self.other,lead,'filtered')
        self.mark(self.u,sent_old,'sent');self.mark(self.u,recent,'filtered');self.mark(self.u,fresh,'filtered')
        with self.db:self.db.execute('UPDATE user_preferences SET delivery_cursor=?',(fresh,))
        cursors=self.cursors()
        kept={(self.u,sent_old,'sent'),(self.u,recent,'filtered'),(self.u,fresh,'filtered')}
        self.assertEqual(cleanup_filtered(self.db,CONFIG,batch_size=3,max_batches=2),6)   # bounded run
        self.assertEqual(len(self.rows()-kept),4)
        self.assertEqual(cleanup_filtered(self.db,CONFIG,batch_size=3,max_batches=10),4)  # the rest, next run
        self.assertEqual(self.db.connection.info.transaction_status.name,'IDLE')          # no transaction left open
        self.assertEqual(self.rows(),kept)
        self.assertEqual(self.cursors(),cursors)                    # trigger skipped: no reset, updated_at untouched
        self.assertEqual(cleanup_filtered(self.db,CONFIG),0)

    def test_filter_change_delete_still_resets_the_cursor(self):
        lead=self.add(1,150);self.mark(self.u,lead,'filtered')
        with self.db:self.db.execute('UPDATE user_preferences SET delivery_cursor=? WHERE user_id=?',(lead,self.u))
        fresh=self.add(2,1);self.mark(self.u,fresh,'filtered')
        self.assertEqual(cleanup_filtered(self.db,CONFIG),1)
        self.assertEqual(self.cursors()[self.u][0],lead)            # the cleanup kept it
        # The setting is transaction-local: a later filter-change DELETE on the same connection resets as before.
        with self.db:self.db.execute("DELETE FROM user_leads WHERE user_id=? AND delivery_status='filtered'",(self.u,))
        self.assertEqual(self.cursors()[self.u][0],0)

    def test_cleanup_does_not_trigger_redelivery(self):
        old=self.add(1,150);fresh=self.add(2,1)
        with self.db:self.db.execute("UPDATE user_preferences SET topics='[\"Боты\"]'::jsonb WHERE user_id=?",(self.u,))
        self.mark(self.u,old,'filtered')
        sent=[]
        with patch.dict(os.environ,{'LEAD_CURSOR_LAG_SECONDS':'60'}):
            product.deliver_registered(self.db,CONFIG,lambda method,data:sent.append(data),limit_per_user=10)
            before=self.cursors()[self.u][0]
            self.assertGreaterEqual(before,fresh-1)
            self.assertEqual(cleanup_filtered(self.db,CONFIG),1)
            self.assertEqual(self.cursors()[self.u][0],before)
            product.deliver_registered(self.db,CONFIG,lambda method,data:sent.append(data),limit_per_user=10)
        self.assertEqual(sent,[])

    def test_fk_indexes_exist_and_are_valid(self):
        for name in ('lead_assignments_owner_lead','telegram_replies_owner_lead','project_leads_lead'):
            row=self.db.execute('''SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='leadgen' AND c.relname=?''',(name,)).fetchone()
            self.assertTrue(row and row['indisvalid'],name)
        self.db.release()

    # --- retention of rejected / duplicate leads -------------------------------------------------------------

    def lead(self,n,hours,status='rejected'):
        lead=self.add(n,hours)
        with self.db:self.db.execute('UPDATE leads SET status=? WHERE id=?',(status,lead))
        return lead

    def lead_ids(self):
        return {row['id'] for row in self.db.execute('SELECT id FROM leads').fetchall()}

    def protect_with_every_kind_of_work(self,start):
        """One old rejected lead per kind of user work; returns their ids."""
        kept=[]
        def lead():
            kept.append(self.lead(start+len(kept),150));return kept[-1]
        def filtered():
            lid=lead()
            self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'filtered')",(self.u,lid))
            return lid
        with self.db:
            self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'sent')",(self.u,lead()))
            for column,value in (('pipeline_status',"'won'"),('crm_note',"'позвонить'"),('deal_amount','1000'),
                                 ('replied_at','now()'),('won_at','now()')):
                self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status,"+column+") VALUES(?,?,'filtered',"
                                +value+")",(self.u,lead()))
            self.db.execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'note','x')",(self.u,filtered()))
            self.db.execute('INSERT INTO lead_reminders(user_id,lead_id,due_at) VALUES(?,?,now())',(self.u,filtered()))
            self.db.execute("INSERT INTO lead_tags(user_id,lead_id,tag) VALUES(?,?,'vip')",(self.u,filtered()))
            self.db.execute('INSERT INTO lead_assignments(workspace_id,owner_user_id,lead_id,assignee_user_id,assigned_by) '
                            'VALUES(?,?,?,?,?)',(self.ws,self.u,filtered(),self.u,self.u))
            self.db.execute('INSERT INTO telegram_replies(id,actor_id,owner_id,lead_id,mode,body,source_url,target_cipher,'
                            'sender_label,recipient_label,account_id,random_id,expires_at) '
                            "VALUES(?,?,?,?,'dm','привет','u','c','s','r',1,1,now())",('a'*32,self.u,self.u,filtered()))
            self.db.execute('INSERT INTO ai_offer_generations(workspace_id,owner_user_id,lead_id,requested_by,model,'
                            "prompt_fingerprint) VALUES(?,?,?,?,'m',?)",(self.ws,self.u,filtered(),self.u,'f'*64))
            self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'not_service')",(self.u,lead()))
            project=self.db.execute("INSERT INTO projects(user_id,workspace_id,name) VALUES(?,?,'Сайты') RETURNING id",
                                    (self.u,self.ws)).fetchone()['id']
            self.db.execute('INSERT INTO project_leads(project_id,user_id,lead_id) VALUES(?,?,?)',(project,self.u,lead()))
        return kept

    def test_retention_deletes_only_unworked_old_rejected_and_duplicate_leads(self):
        dead=[self.lead(1,150),self.lead(2,150,'duplicate'),self.lead(3,200)]
        for lead in dead[:2]:
            self.mark(self.u,lead,'filtered');self.mark(self.other,lead,'filtered')
        with self.db:self.db.execute("INSERT INTO lead_ai_scores(lead_id,status) VALUES(?,'pending')",(dead[0],))
        kept=[self.lead(10+i,150,status) for i,status in enumerate(('ready','review','sent','sending','uncertain'))]
        kept+=self.protect_with_every_kind_of_work(20)
        kept+=[self.lead(40,80),self.lead(41,1)]                  # inside the window + margin (98 h): per-user re-check
        fresh=self.lead(42,1,'ready')
        with self.db:self.db.execute('UPDATE user_preferences SET delivery_cursor=?',(fresh,))
        cursors=self.cursors()
        before=self.lead_ids()
        with patch.dict(os.environ,{'LEAD_REJECTED_RETENTION_DAYS':'3'}):
            self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG,batch_size=2,max_batches=1),2)  # bounded
            self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG,batch_size=2,max_batches=10),1)
            self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG),0)
        self.assertEqual(self.lead_ids(),before-set(dead))
        self.assertTrue(set(kept)<=self.lead_ids())
        self.assertIsNone(self.db.execute('SELECT 1 FROM user_leads WHERE lead_id = ANY(?)',(dead,)).fetchone())
        self.assertIsNone(self.db.execute('SELECT 1 FROM lead_ai_scores WHERE lead_id=?',(dead[0],)).fetchone())
        self.assertEqual(self.cursors(),cursors)                   # cascade of filtered rows kept every cursor
        for table in ('lead_activity','lead_reminders','lead_tags','lead_assignments','telegram_replies',
                      'ai_offer_generations','lead_feedback','project_leads'):
            self.assertIsNotNone(self.db.execute('SELECT 1 FROM '+table).fetchone(),table)
        self.db.release()
        self.assertEqual(self.db.connection.info.transaction_status.name,'IDLE')

    def test_retention_age_follows_the_environment(self):
        lead=self.lead(1,150)
        with patch.dict(os.environ,{'LEAD_REJECTED_RETENTION_DAYS':'10'}):
            self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG),0)
        with patch.dict(os.environ,{'LEAD_REJECTED_RETENTION_DAYS':'0'}):
            self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG),0)
        self.assertIn(lead,self.lead_ids())
        self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG),1)
        self.db.release()

    def test_locked_lead_is_skipped_not_waited_for(self):
        busy,free=self.lead(1,150),self.lead(2,150)
        other=psycopg.connect(fixtures.DSN)
        try:
            other.execute('SELECT 1 FROM leadgen.leads WHERE id=%s FOR UPDATE',(busy,))
            self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG),1)
        finally:
            other.rollback();other.close()
        self.assertEqual(self.lead_ids()&{busy,free},{busy})
        self.db.release()

    def test_rescraped_deleted_lead_is_not_delivered_again(self):
        from leadgen.app import ingest
        from leadgen.core import Lead
        published=(datetime.now(timezone.utc)-timedelta(hours=150)).isoformat()
        lead=Lead(source='FL.ru',url='https://www.fl.ru/projects/1/',title='Нужен сайт для клиники',
                  text='Нужен сайт для клиники, бюджет 80 000 рублей',published=published,budget_text='80 000 рублей')
        with self.db:ingest(self.db,lead,CONFIG)
        old=self.db.execute('SELECT id,status,reason FROM leads WHERE url=?',(lead.url,)).fetchone()
        self.assertEqual(old['status'],'rejected')
        with self.db:self.db.execute("UPDATE leads SET first_seen=now()-interval '150 hours' WHERE id=?",(old['id'],))
        self.assertEqual(filtered_cleanup.cleanup_rejected(self.db,CONFIG),1)
        with self.db:ingest(self.db,lead,CONFIG)                   # the 5-minute re-scan finds it again
        again=self.db.execute('SELECT id,status,reason FROM leads WHERE url=?',(lead.url,)).fetchone()
        self.assertGreater(again['id'],old['id'])
        self.assertEqual((again['status'],again['reason']),(old['status'],old['reason']))
        sent=[]
        with patch.dict(os.environ,{'LEAD_CURSOR_LAG_SECONDS':'60'}):
            with self.db:self.db.execute("UPDATE leads SET first_seen=now()-interval '2 minutes' WHERE id=?",(again['id'],))
            product.deliver_registered(self.db,CONFIG,lambda method,data:sent.append(data),limit_per_user=10)
        self.assertEqual(sent,[])

    def test_run_stats_are_saved_and_shown_in_status(self):
        import contextlib
        import io
        from leadgen import app
        self.lead(1,150)
        timer=FilteredCleanup(interval_seconds=3600)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(timer.run_if_due(self.db,CONFIG),{'filtered':0,'leads':1})
        self.assertIn('отклонённых лидов 1',out.getvalue())
        stats=filtered_cleanup.last_stats(self.db)
        self.assertEqual((stats['filtered'],stats['leads']),(0,1))
        with patch('leadgen.telegram_monitor.reader_status',return_value={}),\
                contextlib.redirect_stdout(io.StringIO()) as out:
            app.status(self.db)
        self.assertEqual(json.loads(out.getvalue())['cleanup']['leads'],1)
        self.db.release()

    def test_cascade_reaches_assignments_and_replies(self):
        lead=self.add(1,150);self.mark(self.u,lead,'filtered')
        with self.db:
            self.db.execute('''INSERT INTO lead_assignments(workspace_id,owner_user_id,lead_id,assignee_user_id,assigned_by)
                VALUES(?,?,?,?,?)''',(self.ws,self.u,lead,self.u,self.u))
        self.assertEqual(cleanup_filtered(self.db,CONFIG),1)
        self.assertIsNone(self.db.execute('SELECT 1 FROM lead_assignments WHERE lead_id=?',(lead,)).fetchone())
        self.db.release()


if __name__=='__main__':
    unittest.main()
