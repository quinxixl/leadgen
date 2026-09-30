"""Opt-in integration tests against a disposable local PostgreSQL database only."""
import json
import os
import unittest
from pathlib import Path
from datetime import datetime,timezone
from leadgen.database import db_open
from leadgen.web import create_app
from leadgen.web_auth import approve_login

DSN=os.environ.get('WEB_TEST_DATABASE_URL','')


@unittest.skipUnless(DSN,'WEB_TEST_DATABASE_URL is not configured')
class WebPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from urllib.parse import urlsplit
        url=urlsplit(DSN)
        if url.hostname!='127.0.0.1' or url.port!=55439 or url.path!='/leadfinder_web_test':
            raise RuntimeError('Use only the disposable local test database on port 55439')
        cls.db=db_open(DSN)
        for role in ('anon','authenticated'):
            if not cls.db.execute('SELECT 1 FROM pg_roles WHERE rolname=?',(role,)).fetchone():
                cls.db.execute('CREATE ROLE '+role)
        for filename in ('20260929134042_create_leadgen_schema.sql','20260930092451_add_multiuser_product.sql'):
            cls.db.connection.execute((Path('supabase/migrations')/filename).read_text(),prepare=False)
        if not cls.db.execute("SELECT to_regclass('leadgen.projects') AS name").fetchone()['name']:
            cls.db.connection.execute(Path('supabase/migrations/20260930143416_web_projects_crm.sql').read_text(),prepare=False)
        if not cls.db.execute("SELECT to_regclass('leadgen.lead_reminders') AS name").fetchone()['name']:
            cls.db.connection.execute(Path('supabase/migrations/20260930193638_lead_reminders.sql').read_text(),prepare=False)
        cls.db.commit()
        cls.app=create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'BOT_USERNAME':'test_bot',
            'SESSION_COOKIE_SECURE':False,'DB_FACTORY':lambda:db_open(DSN)})

    @classmethod
    def tearDownClass(cls):cls.db.close()

    def setUp(self):
        with self.db:
            self.db.execute('TRUNCATE app_users CASCADE')
            self.db.execute('DELETE FROM leads')
            self.db.execute('DELETE FROM settings WHERE key LIKE ?',('web_login:%',))
            self.u=self.db.execute("INSERT INTO app_users(telegram_user_id,telegram_chat_id,display_name) VALUES(1,1,'Первый') RETURNING id").fetchone()['id']
            self.other=self.db.execute("INSERT INTO app_users(telegram_user_id,telegram_chat_id,display_name) VALUES(2,2,'Второй') RETURNING id").fetchone()['id']
            for uid in (self.u,self.other):
                self.db.execute('INSERT INTO user_preferences(user_id) VALUES(?)',(uid,))
                self.db.execute('INSERT INTO subscriptions(user_id) VALUES(?)',(uid,))
            self.ids=[]
            for i,uid in enumerate((self.u,self.other)):
                payload=dict(source='Telegram: test',url=f'https://t.me/test/{i}',title='Нужен сайт '+str(i),
                    text='<script>alert(1)</script> Нужен сайт',published=datetime.now(timezone.utc).isoformat())
                lid=self.db.execute('INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?) RETURNING id',
                    (payload['url'],str(i),json.dumps(payload),'ready','запрос',payload['published'])).fetchone()['id']
                self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'sent')",(uid,lid));self.ids.append(lid)
        self.client=self.app.test_client()
        with self.client.session_transaction() as s:s.update(user_id=self.u,csrf='test-csrf')

    def test_render_owner_pages_and_hide_other_user(self):
        for path in ('/app','/app/leads','/app/settings','/app/sources',f'/app/leads/{self.ids[0]}'):
            r=self.client.get(path);self.assertEqual(r.status_code,200,path)
            self.assertNotIn('<script>alert(1)</script>',r.text)
        self.assertNotIn('Нужен сайт 1',self.client.get('/app/leads').text)
        self.assertEqual(self.client.get(f'/app/leads/{self.ids[1]}').status_code,404)
        self.assertEqual(self.client.get('/admin').status_code,403)

    def test_cross_user_mutation_is_denied(self):
        r=self.client.post(f'/app/leads/{self.ids[1]}',data={'csrf':'test-csrf','status':'won','amount':'100'})
        self.assertEqual(r.status_code,404)
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE lead_id=?',(self.ids[1],)).fetchone()['pipeline_status'],'saved')

    def test_status_feedback_amount_and_settings_persist(self):
        self.assertEqual(self.client.post(f'/app/leads/{self.ids[0]}',data={'csrf':'test-csrf','status':'won','amount':'75000','feedback':'fit'}).status_code,302)
        row=self.db.execute('SELECT * FROM user_leads WHERE lead_id=?',(self.ids[0],)).fetchone()
        self.assertEqual(row['deal_amount'],75000);self.assertEqual(row['pipeline_status'],'won')
        self.assertIn('75 000',self.client.get('/app').text)
        r=self.client.post('/app/settings',data={'csrf':'test-csrf','min_budget':'8000','topics':['Сайты'],'active':'on'})
        self.assertEqual(r.status_code,302)
        prefs=self.db.execute('SELECT * FROM user_preferences WHERE user_id=?',(self.u,)).fetchone()
        self.assertEqual(prefs['min_budget'],8000);self.assertEqual(prefs['topics'],['Сайты'])

    def test_login_flow(self):
        c=self.app.test_client();c.get('/login')
        with c.session_transaction() as s:csrf=s['csrf']
        self.assertEqual(c.post('/login',data={'csrf':csrf,'action':'begin'}).status_code,200)
        with c.session_transaction() as s:token=s['login_token']
        approve_login(self.db,token,self.u)
        self.assertEqual(c.post('/login',data={'csrf':csrf,'action':'finish'}).status_code,302)
        self.assertEqual(c.get('/app').status_code,200)

    def test_export_is_scoped(self):
        r=self.client.get('/app/leads/export')
        self.assertEqual(r.status_code,200);self.assertNotIn('Нужен сайт 1',r.text)

    def test_projects_are_owner_scoped_and_notes_persist(self):
        r=self.client.post('/app/projects',data={'csrf':'test-csrf','name':'Разработка'})
        self.assertEqual(r.status_code,302)
        path=r.headers['Location']
        self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.post(path,data={'csrf':'test-csrf','name':'Сайты','min_budget':'5000','min_score':'20','topics':['Сайты'],'keywords':'сайт\nлендинг','enabled':'on'}).status_code,302)
        self.assertEqual(self.client.post(f'/app/leads/{self.ids[0]}/notes',data={'csrf':'test-csrf','note':'Созвон в четверг'}).status_code,302)
        self.assertIn('Созвон в четверг',self.client.get(f'/app/leads/{self.ids[0]}').text)
        with self.client.session_transaction() as s:s['user_id']=self.other
        self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.post(f'/app/leads/{self.ids[0]}/notes',data={'csrf':'test-csrf','note':'Чужое'}).status_code,404)

    def test_two_projects_deliver_one_notification(self):
        from leadgen.product import deliver_registered
        with self.db:
            self.db.execute('DELETE FROM user_leads WHERE user_id=?',(self.u,))
            self.db.execute('UPDATE user_preferences SET monitoring_active=true WHERE user_id=?',(self.u,))
            self.db.execute("UPDATE leads SET first_seen=(now()+interval '1 second')::text")
            for name in ('Первый проект','Второй проект'):
                self.db.execute('''INSERT INTO projects(user_id,name,topics,show_without_budget)
                    VALUES(?,?,?::jsonb,true)''',(self.u,name,json.dumps(['Сайты'])))
        calls=[]
        delivered=deliver_registered(self.db,{'sources':[],'max_age_hours':72,'min_budget':5000},lambda *args:calls.append(args))
        self.assertEqual(delivered,1);self.assertEqual(len(calls),1)
        count=self.db.execute('SELECT count(*) AS n FROM project_leads WHERE user_id=?',(self.u,)).fetchone()['n']
        self.assertEqual(count,2)

    def test_export_excel_and_filters_are_owner_scoped(self):
        import io
        from openpyxl import load_workbook
        lid=self.ids[0]
        with self.db:
            self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')",(self.u,lid))
            payload=json.loads(self.db.execute('SELECT payload FROM leads WHERE id=?',(lid,)).fetchone()['payload'])
            payload['title']='=HYPERLINK("https://example.com")'
            self.db.execute('UPDATE leads SET payload=? WHERE id=?',(json.dumps(payload),lid))
        r=self.client.get('/app/leads/export?format=xlsx&feedback=fit&source=Telegram:+test')
        self.assertEqual(r.status_code,200)
        sheet=load_workbook(io.BytesIO(r.data)).active
        self.assertEqual(sheet.max_row,2)
        self.assertNotEqual(sheet['A2'].data_type,'f')
        self.assertTrue(sheet['A2'].value.startswith("'="))
        self.assertEqual(sheet.freeze_panes,'A2')
        r=self.client.get('/app/leads/export?format=xlsx&feedback=ad')
        self.assertEqual(load_workbook(io.BytesIO(r.data)).active.max_row,1)
        self.assertEqual(self.client.get('/app/leads?from=bad').status_code,400)
        self.assertEqual(self.client.get('/app/leads?from=2026-09-30&to=2026-09-01').status_code,400)
        self.assertEqual(self.client.get('/app/leads?source=missing').status_code,200)
        self.assertEqual(self.client.get('/app/leads/export?from=2099-01-01').text.count('Нужен сайт'),0)

    def test_reminder_delivery_retry_and_ownership(self):
        from datetime import timedelta
        from leadgen.reminders import schedule,cancel,deliver_due
        future=datetime.now(timezone.utc)+timedelta(hours=1)
        lid=self.ids[0]
        with self.assertRaises(ValueError):schedule(self.db,self.u,self.ids[1],future)
        with self.assertRaises(ValueError):schedule(self.db,self.u,lid,future-timedelta(days=1))
        schedule(self.db,self.u,lid,future,'Уточнить задачу')
        calls=[]
        self.assertEqual(deliver_due(self.db,lambda *args:calls.append(args)),0)
        self.assertEqual(self.client.post(f'/app/leads/{self.ids[1]}/reminder',data={'csrf':'test-csrf','action':'cancel'}).status_code,404)
        with self.db:self.db.execute("UPDATE lead_reminders SET due_at=now()-interval '1 second' WHERE user_id=?",(self.u,))
        self.assertEqual(deliver_due(self.db,lambda *args:calls.append(args)),1)
        self.assertEqual(deliver_due(self.db,lambda *args:calls.append(args)),0)
        self.assertEqual(len(calls),1)
        self.assertEqual(str(calls[0][1]['chat_id']),'1')
        self.assertIn('Уточнить задачу',calls[0][1]['text'])
        schedule(self.db,self.u,lid,future)
        with self.db:self.db.execute("UPDATE lead_reminders SET due_at=now()-interval '1 second' WHERE user_id=?",(self.u,))
        def fail(*args):raise RuntimeError('network')
        self.assertEqual(deliver_due(self.db,fail),0)
        self.assertEqual(self.db.execute('SELECT status FROM lead_reminders WHERE user_id=?',(self.u,)).fetchone()['status'],'uncertain')
        self.assertEqual(deliver_due(self.db,lambda *args:calls.append(args)),0)
        self.assertIn('доставка не подтверждена',self.client.get(f'/app/leads/{lid}').text)
        self.assertTrue(cancel(self.db,self.u,lid))
        self.assertFalse(cancel(self.db,self.other,lid))
        response=self.client.post(f'/app/leads/{lid}/reminder',data={'csrf':'test-csrf','due':future.strftime('%Y-%m-%dT%H:%M')})
        self.assertEqual(response.status_code,302)

    def test_analytics_periods_and_revenue_are_scoped(self):
        with self.db:
            self.db.execute("UPDATE user_leads SET pipeline_status='won',deal_amount=77001 WHERE user_id=?",(self.u,))
            self.db.execute("UPDATE user_leads SET pipeline_status='won',deal_amount=998877 WHERE user_id=?",(self.other,))
        page=self.client.get('/app?days=7')
        self.assertEqual(page.status_code,200)
        self.assertIn('77 001',page.text)
        self.assertNotIn('998 877',page.text)
        with self.db:self.db.execute("UPDATE leads SET first_seen=(now()-interval '20 days')::text WHERE id=?",(self.ids[0],))
        self.assertNotIn('77 001',self.client.get('/app?days=7').text)
        self.assertIn('77 001',self.client.get('/app?days=30').text)
        self.assertEqual(self.client.get('/app?days=1000').status_code,400)

    def test_duplicate_does_not_send_again_to_user(self):
        from leadgen.product import deliver_registered
        from test_leadgen import CONFIG
        payload=json.loads(self.db.execute('SELECT payload FROM leads WHERE id=?',(self.ids[0],)).fetchone()['payload'])
        payload.update(url='https://t.me/another/1',title='Нужен бот',text='Нужен бот, бюджет 20000 рублей',budget_text='20000 руб.')
        with self.db:
            self.db.execute("UPDATE user_preferences SET monitoring_active=true,show_without_budget=true WHERE user_id=?",(self.u,))
            self.db.execute('INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?)',
                (payload['url'],'0',json.dumps(payload),'ready','test',datetime.now(timezone.utc).isoformat()))
        with self.db:self.db.execute("UPDATE leads SET status='duplicate' WHERE id=?",(self.ids[1],))
        sent=[]
        self.assertEqual(deliver_registered(self.db,CONFIG|{'sources':[]},lambda *a:sent.append(a)),0)
        self.assertEqual(sent,[])
