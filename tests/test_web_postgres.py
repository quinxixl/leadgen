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
