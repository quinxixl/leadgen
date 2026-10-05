import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import Channel, User, InputPeerUser, InputPeerChannel, InputPeerSelf
from leadgen.telegram_replies import ReplyGateway, source_message
from leadgen.telegram_accounts import TelegramAccountError


class ReplyTargets(unittest.TestCase):
    def test_only_message_links(self):
        self.assertEqual(source_message('https://t.me/business_chat/12'), ('business_chat',12))
        self.assertEqual(source_message('https://t.me/s/business_chat/12'), ('business_chat',12))
        self.assertEqual(source_message('https://t.me/c/1234/56?single'), (-1001234,56))
        for url in ('https://evil.test/x/1','https://t.me/test','https://t.me/+invite',
                    'https://t.me/test/0','https://t.me/test/12/34','https://t.me/test/1#x'):
            with self.assertRaises(TelegramAccountError):source_message(url)

    def test_dm_rejects_hidden_bot_deleted_and_self(self):
        gateway=object.__new__(ReplyGateway)
        for sender in (Channel(3,'Канал',photo=None,date=None), User(3,bot=True),
                       User(3,deleted=True),User(1)):
            client=self.client(sender)
            with self.assertRaises(TelegramAccountError):
                asyncio.run(gateway._resolve(client,'https://t.me/business_chat/12','dm'))

    def client(self,sender=None):
        client=SimpleNamespace(is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=User(1,first_name='Я')),
            get_entity=AsyncMock(return_value=Channel(5,'Группа',photo=None,date=None,megagroup=True)),
            get_input_entity=AsyncMock(return_value=InputPeerUser(3,4)))
        client.get_messages=AsyncMock(return_value=SimpleNamespace(message='Нужен сайт',
            get_sender=AsyncMock(return_value=sender or User(3,first_name='Заказчик',username='buyer'))))
        return client

    def test_dm_resolves_actual_author_not_text_contact(self):
        gateway=object.__new__(ReplyGateway);client=self.client()
        info,_=asyncio.run(gateway._resolve(client,'https://t.me/business_chat/12','dm'))
        self.assertEqual(info['recipient_id'],3)
        self.assertEqual(info['recipient'],'Заказчик (@buyer)')
        self.assertEqual(info['source_peer'],-1000000000005)

    def test_no_group_reply_to_broadcast_or_missing_message(self):
        gateway=object.__new__(ReplyGateway);client=self.client()
        client.get_entity.return_value=Channel(5,'Канал',photo=None,date=None,broadcast=True)
        with self.assertRaises(TelegramAccountError):
            asyncio.run(gateway._resolve(client,'https://t.me/business_chat/12','group'))
        client.get_messages.return_value=None
        with self.assertRaises(TelegramAccountError):
            asyncio.run(gateway._resolve(client,'https://t.me/business_chat/12','dm'))

    def test_send_pins_recipient_random_id_and_group_reply(self):
        class Client:
            connect=AsyncMock();disconnect=AsyncMock()
            def __init__(self):self.requests=[]
            async def __call__(self,request):self.requests.append(request)
        gateway=object.__new__(ReplyGateway);client=Client()
        gateway._client=lambda session:client
        info=dict(account_id=1,source_peer=-1000000000005,message_id=12,recipient_id=-1000000000005)
        gateway._resolve=AsyncMock(return_value=(info,InputPeerChannel(5,6)))
        asyncio.run(gateway._send('session','url','group','Привет',info,123))
        request=client.requests[0]
        self.assertEqual(request.random_id,123);self.assertEqual(request.reply_to.reply_to_msg_id,12)
        self.assertIsInstance(request.send_as,InputPeerSelf)
        self.assertIsNone(request.entities);self.assertEqual(request.message,'Привет')
        with self.assertRaises(TelegramAccountError):
            asyncio.run(gateway._send('session','url','dm','Привет',dict(info,recipient_id=9),124))
        self.assertEqual(len(client.requests),1)
