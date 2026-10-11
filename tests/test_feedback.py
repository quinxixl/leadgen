"""User feedback and beta surveys on a real isolated PostgreSQL with a fake Telegram sender."""
import os
import unittest
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import feedback as fb
from leadgen.product import FEEDBACK_ENTRY, HELP, ProductController, menu

CONFIG={'min_budget':5000,'max_age_hours':72,'sources':[]}


def buttons(markup):
    return [button for row in (markup or {}).get('inline_keyboard',[]) for button in row]


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class Feedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        with self.db:self.db.execute("UPDATE app_users SET terms_accepted_at=now()")
        self.env=patch.dict(os.environ,{'FEEDBACK_CHAT_ID':'777','WEB_ADMIN_TELEGRAM_IDS':'','WEB_PUBLIC_URL':''});self.env.start()
        self.sent=[];self.ui=ProductController(self.db,CONFIG,lambda method,data:self.sent.append((method,data)))
        self.clicks=0

    def tearDown(self):self.env.stop()

    def text(self,value,uid=1):
        self.ui.handle({'message':{'from':{'id':uid,'first_name':'Тест'},'chat':{'id':uid,'type':'private'},'text':value}})

    def callback(self,data,uid=1):
        self.clicks+=1
        self.ui.handle({'callback_query':{'id':f'cb{self.clicks}','from':{'id':uid,'first_name':'Тест'},'data':data,
            'message':{'message_id':500,'chat':{'id':uid,'type':'private'}}}})

    def messages(self):return [data for name,data in self.sent if name=='sendMessage']

    def rows(self):
        return self.db.execute('SELECT * FROM feedback ORDER BY id').fetchall()

    def test_bot_feedback_flow_from_help_and_command(self):
        self.text(HELP)
        self.assertIn('fbk:start',[b.get('callback_data') for b in buttons(self.messages()[-1]['reply_markup'])])
        # The six-button main menu stays unchanged.
        self.assertEqual(sum(len(row) for row in menu()['keyboard']),6)
        self.callback('fbk:start')
        self.assertEqual([b['callback_data'] for b in buttons(self.messages()[-1]['reply_markup'])],['fbk:idea','fbk:bug','fbk:other'])
        self.callback('fbk:bug');self.text('Не открывается карточка лида')
        self.assertIn('Спасибо',self.messages()[-1]['text'])
        row=self.rows()[-1]
        self.assertEqual((row['kind'],row['source'],row['text'],row['user_id'],row['status']),('bug','bot','Не открывается карточка лида',self.u,'new'))
        self.text('/feedback');self.callback('fbk:idea');self.text('/cancel')
        self.assertEqual(len(self.rows()),1)
        self.text(FEEDBACK_ENTRY);self.callback('fbk:other');self.text('Вопрос')
        self.assertEqual([r['kind'] for r in self.rows()],['bug','other'])

    def test_notifications_are_sent_once_and_failures_keep_feedback(self):
        fb.save(self.db,self.u,'idea','Добавьте экспорт',source='web',context='/app/leads')
        fb.save(self.db,self.other,'bug','Ошибка',source='miniapp')
        def broken(method,data):raise RuntimeError('Telegram: down')
        self.assertEqual(fb.deliver_notifications(self.db,broken,limit=1),0)
        sent=[];self.assertEqual(fb.deliver_notifications(self.db,lambda m,d:sent.append(d)),1)
        self.assertEqual(sent[0]['chat_id'],'777');self.assertIn('Ошибка',sent[0]['text'])
        self.assertEqual(fb.deliver_notifications(self.db,lambda m,d:sent.append(d)),0)
        self.assertEqual(len(self.rows()),2)
        with patch.dict(os.environ,{'FEEDBACK_CHAT_ID':'','WEB_ADMIN_TELEGRAM_IDS':'11,12'}):
            fb.save(self.db,self.u,'other','Ещё',source='bot')
            self.assertEqual(fb.deliver_notifications(self.db,lambda m,d:sent.append(d)),2)
        self.assertEqual([d['chat_id'] for d in sent[1:]],['11','12'])

    def test_web_and_miniapp_forms_csrf_rate_limit_and_admin(self):
        page=self.client.get('/app/feedback',headers={'Referer':'http://localhost/app/leads?q=secret'})
        self.assertEqual(page.status_code,200);self.assertIn('value="/app/leads"',page.text)
        self.assertIn('href="/app/feedback"',self.client.get('/app').text)
        self.assertEqual(self.client.post('/app/feedback',data={'kind':'idea','text':'x'}).status_code,400)
        r=self.client.post('/app/feedback',data={'csrf':'test-csrf','kind':'idea','text':'Нужен тёмный режим','context':'/app/leads'})
        self.assertEqual(r.status_code,302)
        row=self.rows()[-1]
        self.assertEqual((row['source'],row['kind'],row['context'],row['workspace_id']),('web','idea','/app/leads',self.ws))
        self.assertEqual(self.client.post('/app/feedback',data={'csrf':'test-csrf','kind':'nope','text':'x'}).status_code,400)
        with self.client.session_transaction('/tg/feed') as state:state.update(user_id=self.u,csrf='test-csrf')
        tg=self.client.get('/tg/feedback');self.assertEqual(tg.status_code,200)
        self.assertIn('/tg/feedback',self.client.get('/tg/settings').text)
        self.assertEqual(self.client.post('/tg/feedback',data={'csrf':'test-csrf','kind':'bug','text':'Кнопка не работает'}).status_code,302)
        self.assertEqual(self.rows()[-1]['source'],'miniapp')
        for i in range(3):
            self.assertEqual(self.client.post('/app/feedback',data={'csrf':'test-csrf','kind':'other','text':f'm{i}'}).status_code,302)
        limited=self.client.post('/app/feedback',data={'csrf':'test-csrf','kind':'other','text':'sixth'})
        self.assertEqual(limited.status_code,400);self.assertIn('Слишком много',limited.text)
        self.assertEqual(len(self.rows()),5)
        fid=self.rows()[0]['id']
        self.assertEqual(self.client.post(f'/admin/feedback/{fid}',data={'csrf':'test-csrf','status':'done'}).status_code,403)
        with patch.dict(os.environ,{'WEB_ADMIN_TELEGRAM_IDS':'1'}):
            admin=self.client.get('/admin');self.assertIn('Нужен тёмный режим',admin.text)
            self.assertEqual(self.client.post(f'/admin/feedback/{fid}',data={'csrf':'test-csrf','status':'done'}).status_code,302)
            self.assertEqual(self.client.post(f'/admin/feedback/{fid}',data={'csrf':'test-csrf','status':'bad'}).status_code,400)
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_beta_surveys_sent_once_and_answers_stored(self):
        with self.db:
            self.db.execute("UPDATE app_users SET registered_at=now()-interval '8 days' WHERE id=?",(self.u,))
            self.db.execute("UPDATE app_users SET registered_at=now()-interval '2 days' WHERE id=?",(self.other,))
        sent=[];send=lambda m,d:sent.append(d)
        self.assertEqual(fb.send_surveys(self.db,send),1)
        self.assertEqual(sent[0]['chat_id'],'1');self.assertIn(fb.SURVEY_Q1,sent[0]['text'])
        self.assertEqual([b['text'] for b in buttons(sent[0]['reply_markup'])],['Очень','Немного','Не расстроюсь'])
        self.assertEqual(fb.send_surveys(self.db,send),0)
        self.callback('sv:d7:a:very')
        q2=self.messages()[-1];self.assertEqual(q2['text'],fb.SURVEY_Q2)
        self.assertEqual([b['text'] for b in buttons(q2['reply_markup'])],['0','до 500','500–1000','1000–2000','больше 2000'])
        self.callback('sv:d7:p:500_1000')
        self.assertEqual(fb.deliver_notifications(self.db,send),0)
        self.text('Больше чатов по дизайну')
        row=self.db.execute("SELECT * FROM feedback WHERE survey='d7'").fetchone()
        self.assertEqual((row['kind'],row['disappointment'],row['price'],row['text']),('survey','very','500_1000','Больше чатов по дизайну'))
        self.assertIsNotNone(row['answered_at'])
        self.assertEqual(fb.deliver_notifications(self.db,send),1);self.assertIn('Опрос беты',sent[-1]['text'])
        # Unknown survey or a survey never sent changes nothing.
        self.callback('sv:d21:a:very');self.assertIsNone(self.db.execute("SELECT 1 FROM feedback WHERE survey='d21'").fetchone())
        # Day 21: the last survey must be at least three days old; old users skip the day-7 survey.
        with self.db:
            self.db.execute("UPDATE app_users SET registered_at=now()-interval '22 days'")
            self.db.execute("UPDATE feedback SET created_at=now()-interval '14 days'")
        self.assertEqual(fb.send_surveys(self.db,send),2)
        surveys=self.db.execute("SELECT user_id,survey FROM feedback WHERE kind='survey' ORDER BY user_id,survey").fetchall()
        self.assertEqual([(r['user_id'],r['survey']) for r in surveys],[(self.u,'d21'),(self.u,'d7'),(self.other,'d21')])
        self.callback('sv:d21:a:not',uid=2);self.callback('sv:d21:p:0',uid=2);self.callback('sv:d21:skip',uid=2)
        row=self.db.execute("SELECT * FROM feedback WHERE survey='d21' AND user_id=?",(self.other,)).fetchone()
        self.assertEqual((row['disappointment'],row['price'],row['text']),('not','0',''));self.assertIsNotNone(row['answered_at'])


if __name__=='__main__':unittest.main()
