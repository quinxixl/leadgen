"""No live Telegram writes: real isolated PostgreSQL, fake Telegram boundary."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import telegram_replies as replies
from leadgen.database import db_open
from leadgen.product import ProductController
from leadgen.telegram_accounts import SessionCipher, TelegramAccountError


class Gateway:
    def __init__(self):self.sent=[];self.previews=[];self.error=None;self.during_send=None
    def preview(self,session,url,mode):
        self.previews.append((session,url,mode))
        return dict(account_id=int(session),sender='Мой аккаунт',source_peer=-1000000000005,
                    message_id=101,recipient_id=77 if mode=='dm' else -1000000000005,
                    recipient='Автор' if mode=='dm' else 'Группа')
    def send(self,*args):
        self.sent.append(args)
        if self.during_send:self.during_send()
        if self.error:raise self.error


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class ReplyPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        self.cipher=SessionCipher(self.app.config['TELEGRAM_CIPHER_KEY'])
        self.gateway=Gateway()
        self.app.config['TELEGRAM_REPLY_GATEWAY_FACTORY']=lambda:self.gateway
        with self.db:
            for uid,account in ((self.u,9001),(self.other,9002)):
                self.db.execute('''INSERT INTO telegram_connections
                    (user_id,telegram_user_id,session_cipher,status,display_name) VALUES(?,?,?,'active','Мой аккаунт')''',
                    (uid,account,self.cipher.encrypt({'session':str(account)})))
            row=self.db.execute('SELECT payload FROM leads WHERE id=?',(self.ids[0],)).fetchone()
            payload=json.loads(row['payload']);payload['url']='https://t.me/business_chat/101'
            self.db.execute('UPDATE leads SET payload=?,url=? WHERE id=?',(json.dumps(payload),payload['url'],self.ids[0]))

    def prepare(self,mode='dm',body='Здравствуйте! Готов обсудить сайт.'):
        return replies.prepare(self.db,self.u,self.u,self.ids[0],mode,body,self.cipher,self.gateway)

    def test_web_preview_then_confirm_both_modes_and_repeat(self):
        path=f'/app/leads/{self.ids[0]}/reply'
        self.assertIn('Ответить через Telegram',self.client.get(f'/app/leads/{self.ids[0]}').text)
        for mode in ('dm','group'):
            response=self.client.post(path,data={'csrf':'test-csrf','mode':mode,'body':'Текст <b>без HTML</b>'})
            self.assertEqual(response.status_code,302)
            location=response.location;page=self.client.get(location)
            self.assertIn('Подтвердить и отправить',page.text)
            self.assertIn('&lt;b&gt;',page.text)
            before=len(self.gateway.sent)
            self.client.post(location,data={'csrf':'test-csrf','action':'send'})
            self.client.post(location,data={'csrf':'test-csrf','action':'send'})
            self.assertEqual(len(self.gateway.sent),before+1)
            self.assertEqual(self.gateway.sent[-1][0],'9001')
            self.assertEqual(self.gateway.sent[-1][2],mode)
            self.assertIn('Отправлено',self.client.get(location).text)
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE user_id=? AND lead_id=?',
            (self.u,self.ids[0])).fetchone()['pipeline_status'],'contacted')

    def test_cancel_expiry_missing_account_and_invalid_text(self):
        row=self.prepare();replies.cancel(self.db,row['id'],self.u,self.u)
        result=replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        self.assertEqual(result['status'],'cancelled');self.assertFalse(self.gateway.sent)
        row=self.prepare()
        with self.db:self.db.execute("UPDATE telegram_replies SET expires_at=now()-interval '1 second' WHERE id=?",(row['id'],))
        with self.assertRaises(TelegramAccountError):replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        for text in ('',' '*10,'😀'*1501):
            with self.assertRaises(TelegramAccountError):self.prepare(body=text)
        with self.db:self.db.execute("UPDATE telegram_connections SET status='revoked' WHERE user_id=?",(self.u,))
        with self.assertRaises(TelegramAccountError):self.prepare()
        self.assertFalse(self.gateway.sent)

    def test_foreign_lead_reply_and_csrf_blocked(self):
        self.assertEqual(self.client.post(f'/app/leads/{self.ids[1]}/reply',data={
            'csrf':'test-csrf','body':'Чужой','mode':'dm'}).status_code,404)
        self.assertFalse(self.gateway.previews)
        row=self.prepare()
        self.assertEqual(self.client.post('/app/replies/'+row['id'],data={'action':'send'}).status_code,400)
        with self.client.session_transaction() as state:state.update(user_id=self.other,workspace_id=self.other_ws)
        self.assertEqual(self.client.get('/app/replies/'+row['id']).status_code,404)
        self.assertFalse(self.gateway.sent)

    def test_failure_and_timeout_never_resend_or_mark_contacted(self):
        for error,status in ((TelegramAccountError('Нет права писать'),'failed'),(TimeoutError(),'uncertain')):
            row=self.prepare();self.gateway.error=error
            result=replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
            self.assertEqual(result['status'],status)
            before=len(self.gateway.sent)
            replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
            self.assertEqual(len(self.gateway.sent),before)
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE user_id=? AND lead_id=?',
            (self.u,self.ids[0])).fetchone()['pipeline_status'],'saved')

    def test_claim_is_committed_before_send_for_other_workers(self):
        row=self.prepare();other_db=db_open(fixtures.DSN)
        try:
            def second_click():
                result=replies.confirm(other_db,row['id'],self.u,self.u,self.cipher,self.gateway)
                self.assertEqual(result['status'],'sending')
            self.gateway.during_send=second_click
            result=replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
            self.assertEqual(result['status'],'sent');self.assertEqual(len(self.gateway.sent),1)
        finally:other_db.close()

    def test_team_uses_actor_account_and_rechecks_role(self):
        with self.db:self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'manager')",(self.ws,self.other))
        with self.client.session_transaction() as state:state.update(user_id=self.other,workspace_id=self.ws)
        response=self.client.post(f'/app/leads/{self.ids[0]}/reply',data={'csrf':'test-csrf','body':'От менеджера','mode':'dm'})
        self.assertEqual(response.status_code,302)
        self.client.post(response.location,data={'csrf':'test-csrf','action':'send'})
        self.assertEqual(self.gateway.sent[-1][0],'9002')
        self.assertEqual(self.gateway.previews[-1][0],'9002')
        row=replies.prepare(self.db,self.other,self.u,self.ids[0],'dm','Ещё текст',self.cipher,self.gateway)
        with self.db:self.db.execute("UPDATE workspace_members SET role='viewer' WHERE user_id=? AND workspace_id=?",(self.other,self.ws))
        self.assertEqual(self.client.post('/app/replies/'+row['id'],data={'csrf':'test-csrf','action':'send'}).status_code,403)
        with self.assertRaises(TelegramAccountError):
            replies.confirm(self.db,row['id'],self.other,self.u,self.cipher,self.gateway)

    def test_account_subscription_and_lead_access_rechecked(self):
        row=self.prepare()
        with self.db:self.db.execute('UPDATE telegram_connections SET telegram_user_id=9003 WHERE user_id=?',(self.u,))
        with self.assertRaises(TelegramAccountError):replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        with self.db:
            self.db.execute('UPDATE telegram_connections SET telegram_user_id=9001 WHERE user_id=?',(self.u,))
            self.db.execute("UPDATE subscriptions SET ends_at=now()-interval '1 second' WHERE user_id=?",(self.u,))
        with self.assertRaises(TelegramAccountError):replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        with self.db:
            self.db.execute("UPDATE subscriptions SET ends_at=now()+interval '1 day' WHERE user_id=?",(self.u,))
            self.db.execute("UPDATE user_leads SET delivery_status='filtered' WHERE user_id=?",(self.u,))
        with self.assertRaises(TelegramAccountError):replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        self.assertFalse(self.gateway.sent)

    def test_bot_compose_preview_confirm_and_cancel(self):
        sent=[];controller=ProductController(self.db,{},lambda method,data:sent.append((method,data)))
        controller._reply_services=lambda:(self.cipher,self.gateway)
        def callback(data):controller.handle({'callback_query':{'id':'click','from':{'id':1},'data':data,
            'message':{'chat':{'id':1,'type':'private'}}}})
        def text(value):controller.handle({'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'text':value}})
        for mode in ('dm','group'):
            callback(f'tgcompose:{self.ids[0]}');callback(f'tgmode:{mode}:{self.ids[0]}')
            text('Здравствуйте! Отклик из бота.')
            preview=sent[-1][1]
            self.assertIn('Проверьте отклик',preview['text'])
            confirm=preview['reply_markup']['inline_keyboard'][0][0]['callback_data']
            before=len(self.gateway.sent);callback(confirm);callback(confirm)
            self.assertEqual(len(self.gateway.sent),before+1)
        callback(f'tgmode:dm:{self.ids[0]}');text('Отменить этот текст')
        confirm=sent[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        text('/cancel');before=len(self.gateway.sent);callback(confirm)
        self.assertEqual(len(self.gateway.sent),before)
        callback(f'tgcompose:{self.ids[1]}')
        self.assertIn('недоступен',sent[-1][1]['text'])

    def test_killed_send_is_recovered_as_uncertain_not_resent(self):
        row=self.prepare()
        with self.db:
            self.db.execute("UPDATE telegram_replies SET status='sending',updated_at=now()-interval '1 minute' WHERE id=?",(row['id'],))
        self.assertEqual(replies.get_reply(self.db,row['id'],self.u,self.u)['status'],'sending')
        with self.db:
            self.db.execute("UPDATE telegram_replies SET updated_at=now()-interval '6 minutes' WHERE id=?",(row['id'],))
        result=replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        self.assertEqual(result['status'],'uncertain');self.assertTrue(result['error'])
        self.assertFalse(self.gateway.sent)

    def test_bot_menu_button_leaves_reply_flow_and_errors_do_not_escape(self):
        sent=[];controller=ProductController(self.db,{},lambda method,data:sent.append((method,data)))
        controller._reply_services=lambda:(self.cipher,self.gateway)
        def callback(data):controller.handle({'callback_query':{'id':'click','from':{'id':1},'data':data,
            'message':{'chat':{'id':1,'type':'private'}}}})
        def text(value):controller.handle({'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'text':value}})
        callback(f'tgmode:dm:{self.ids[0]}');text('📈 Статистика')
        self.assertFalse(self.gateway.previews)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM telegram_replies').fetchone()['n'],0)
        callback(f'tgmode:dm:{self.ids[0]}');text('Текст отклика')
        reply_id=sent[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data'].split(':')[1]
        text('📥 Лиды')
        self.assertEqual(self.db.execute('SELECT status FROM telegram_replies WHERE id=?',(reply_id,)).fetchone()['status'],'cancelled')
        text('🧰 Портфолио');text('🎯 Услуги')
        self.assertNotEqual(self.db.execute('SELECT portfolio FROM user_preferences WHERE user_id=?',(self.u,)).fetchone()['portfolio'],'🎯 Услуги')
        self.assertIn('услуги',sent[-1][1]['text'].lower())
        text('➕ Добавить чат');text('💳 Подписка')
        self.assertNotIn('публичная ссылка',sent[-1][1]['text'])
        def broken():raise KeyError('boom')
        controller._reply_services=broken
        callback(f'tgmode:dm:{self.ids[0]}');text('Ещё текст')
        self.assertIn('Не удалось',sent[-1][1]['text'])

    def test_table_private_and_no_regression_of_advanced_pipeline(self):
        table=self.db.execute("SELECT relrowsecurity FROM pg_class WHERE oid='leadgen.telegram_replies'::regclass").fetchone()
        self.assertTrue(table['relrowsecurity'])
        for role in ('anon','authenticated'):
            self.assertFalse(self.db.execute("SELECT has_table_privilege(?, 'leadgen.telegram_replies','SELECT') AS allowed",(role,)).fetchone()['allowed'])
        with self.db:self.db.execute("UPDATE user_leads SET pipeline_status='won' WHERE user_id=? AND lead_id=?",(self.u,self.ids[0]))
        row=self.prepare();replies.confirm(self.db,row['id'],self.u,self.u,self.cipher,self.gateway)
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE user_id=? AND lead_id=?',
            (self.u,self.ids[0])).fetchone()['pipeline_status'],'won')
