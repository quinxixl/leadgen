import unittest
from types import SimpleNamespace
from leadgen.app import send_lead
from leadgen.telegram_monitor import message_link, realtime_group_username
from test_leadgen import lead

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

    def test_realtime_reader_accepts_public_groups_and_channels_only(self):
        group=SimpleNamespace(username='Business_Chat',broadcast=False)
        event=SimpleNamespace(is_group=True,is_private=False)
        channel_event=SimpleNamespace(is_group=False,is_channel=True,is_private=False)
        self.assertEqual(realtime_group_username(event,group),'business_chat')
        self.assertIsNone(realtime_group_username(SimpleNamespace(is_group=False,is_private=True),group))
        self.assertEqual(realtime_group_username(channel_event,SimpleNamespace(username='News',broadcast=True)),'news')
        self.assertIsNone(realtime_group_username(event,SimpleNamespace(username=None,broadcast=False)))
        self.assertIsNone(realtime_group_username(channel_event,SimpleNamespace(username=None,broadcast=True)))


class Worker(unittest.TestCase):
    def test_bot_requires_postgres(self):
        from leadgen.bot import run_bot
        with self.assertRaisesRegex(ValueError,'PostgreSQL'):
            run_bot(SimpleNamespace(backend='sqlite'),{'sources':[]})
