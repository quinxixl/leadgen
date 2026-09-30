import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from leadgen.app import db_open, ingest, send_lead
from leadgen.bot import Controller
from leadgen.telegram_monitor import message_link, realtime_group_username
from test_leadgen import lead

CONFIG={'min_budget':5000,'max_age_hours':72,'poll_seconds':300,'sources':[
    {'name':'test','id':'test','kind':'telegram','enabled':True},
    {'name':'Avito','id':'avito','kind':'pending','enabled':False}]}

class Buttons(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=db_open(Path(self.tmp.name)/'bot.sqlite')
        self.env=patch.dict(os.environ,{'TELEGRAM_CHAT_ID':'123'});self.env.start()
        self.calls=[];self.sender=lambda *args:self.calls.append(args)
        self.ui=Controller(self.db,CONFIG,self.sender)

    def tearDown(self):
        self.db.close();self.env.stop();self.tmp.cleanup()

    def text(self,text,owner=123):
        self.ui.handle({'message':{'from':{'id':owner},'chat':{'id':owner,'type':'private'},'text':text}})

    def callback(self,data,owner=123):
        self.ui.handle({'callback_query':{'id':'q1','from':{'id':owner},'message':{'chat':{'id':owner,'type':'private'}},'data':data}})

    def test_owner_only(self):
        self.text('▶️ Начать',456);self.callback('budget:1',456)
        self.assertFalse(self.ui.get('active',False));self.assertEqual(self.calls,[])

    def test_notification_recipient_can_confirm_subscription_but_not_manage(self):
        with patch.dict(os.environ,{'TELEGRAM_NOTIFY_CHAT_IDS':'456'}):
            self.text('/start',456)
            self.text('▶️ Начать',456)
        self.assertFalse(self.ui.get('active',False))
        self.assertEqual(self.calls[0][1]['chat_id'],'456')
        self.assertIn('подключён к уведомлениям',self.calls[0][1]['text'])

    def test_pause_and_persistence(self):
        self.text('▶️ Начать');self.assertTrue(self.ui.get('active'))
        self.ui.drain=True;self.text('⏸ Остановить')
        self.assertFalse(self.ui.drain);self.assertFalse(self.ui.manual)
        self.assertFalse(Controller(self.db,CONFIG,self.sender).get('active'))

    def test_budget_rechecks_existing_queue(self):
        with self.db:ingest(self.db,lead(),self.ui.config())
        self.callback('budget:25000')
        self.assertEqual(self.db.execute('SELECT status FROM leads').fetchone()[0],'rejected')
        self.callback('budget:5000')
        self.assertEqual(self.db.execute('SELECT status FROM leads').fetchone()[0],'ready')

    def test_custom_budget(self):
        self.callback('budget:custom');self.text('7 500')
        self.assertEqual(self.ui.config()['min_budget'],7500)

    def test_service_toggle(self):
        self.callback('topic:1');self.assertNotIn('Боты',self.ui.config()['topics'])
        self.callback('topic:1');self.assertIn('Боты',self.ui.config()['topics'])

    def test_source_toggle_and_pending(self):
        self.callback('source:test');self.assertFalse(self.ui.config()['sources'][0]['enabled'])
        self.callback('source:avito');self.assertFalse(self.ui.config()['sources'][1]['enabled'])

    def test_manual_does_not_enable_schedule(self):
        self.text('🔎 Найти сейчас');self.assertTrue(self.ui.manual);self.assertFalse(self.ui.get('active',False))

    def test_list_and_show(self):
        with self.db:ingest(self.db,lead(),self.ui.config())
        self.text('📋 Объявления')
        self.assertIn('Нужен Telegram бот',self.calls[-1][1]['text'])
        self.callback('show:1')
        self.assertIn('Открыть оригинал',json.dumps(self.calls[-1],ensure_ascii=False))

class Forwarding(unittest.TestCase):
    def test_summary_card_with_original_link(self):
        calls=[]
        send_lead(lead(url='https://t.me/testchannel/52'),'reason','123',lambda *args:calls.append(args))
        self.assertEqual(calls[0][0],'sendMessage')
        self.assertIn('лид',calls[0][1]['text'])
        self.assertIn('Нужно:',calls[0][1]['text'])
        self.assertEqual(calls[0][1]['reply_markup']['inline_keyboard'][0][0]['url'],'https://t.me/testchannel/52')

    def test_public_message_link(self):
        self.assertEqual(message_link('business_chat',42),'https://t.me/business_chat/42')

    def test_realtime_reader_accepts_only_public_groups(self):
        group=SimpleNamespace(username='Business_Chat',broadcast=False)
        event=SimpleNamespace(is_group=True,is_private=False)
        self.assertEqual(realtime_group_username(event,group),'business_chat')
        self.assertIsNone(realtime_group_username(SimpleNamespace(is_group=False,is_private=True),group))
        self.assertIsNone(realtime_group_username(event,SimpleNamespace(username='news',broadcast=True)))
        self.assertIsNone(realtime_group_username(event,SimpleNamespace(username=None,broadcast=False)))
