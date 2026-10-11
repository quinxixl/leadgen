import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from leadgen.app import db_open
from leadgen.source_registry import (NOT_JOINED_REASON, apply_monitor_policy, import_selection, inspect_public_page,
                                     is_joined, read_count, verify_pending)


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=db_open(Path(self.tmp.name)/'db.sqlite')
    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_imports_only_approved_public_sources(self):
        data={'selected':[
            {'sheet':'Потенциальные клиенты','url':'https://t.me/good_chat','title':'Good','segment':'Бизнес','priority':1,'members':100},
            {'sheet':'Требуют проверки','url':'https://t.me/review_chat','title':'Review','segment':'Бизнес','priority':1,'members':100},
            {'sheet':'Партнеры и заказы','url':'https://t.me/+private','title':'Private','segment':'Заказы','priority':3,'members':100}]}
        path=Path(self.tmp.name)/'data.json';path.write_text(json.dumps(data))
        result=import_selection(self.db,path)
        self.assertEqual(result['imported'],2);self.assertEqual(result['unsupported_links'],1)
        self.assertEqual(result['review'],1)

    @patch('leadgen.source_registry.fetch')
    def test_active_group_needs_online_audience(self,fetch):
        fetch.return_value=b'''<div class="tgme_page_title">Business chat</div>
            <div class="tgme_page_extra">1 200 members, 83 online</div>
            <a class="tgme_action_button_new">Join Group</a>'''
        result=inspect_public_page('business_chat')
        self.assertEqual(result['status'],'active');self.assertEqual(result['online'],83)

    @patch('leadgen.source_registry.fetch')
    def test_person_is_not_chat(self,fetch):
        fetch.return_value=b'''<div class="tgme_page_title">Ivan</div>
            <div class="tgme_page_extra">@ivan</div><a class="tgme_action_button_new">Send Message</a>'''
        self.assertEqual(inspect_public_page('ivan_public')['status'],'not_chat')

    def add(self,username,group='Потенциальные клиенты',segment='Бизнес',metadata='{}',status='active',
            enabled=1,members=100,online=5):
        with self.db:
            self.db.execute('''INSERT INTO telegram_sources
                (username,url,title,segment,selection_group,members,online,status,enabled,check_reason,metadata)
                VALUES(?,?,?,?,?,?,?,?,?,'83 онлайн при проверке',?)''',
                (username,'https://t.me/'+username,username.title(),segment,group,members,online,status,enabled,metadata))

    def row(self,username):
        return self.db.execute('SELECT * FROM telegram_sources WHERE username=?',(username,)).fetchone()

    def test_joined_marker_sources(self):
        self.assertTrue(is_joined('joined','{}'));self.assertTrue(is_joined('x','{}','joined'))
        self.assertTrue(is_joined('x','{"joined":true}'));self.assertFalse(is_joined('x','{"joined":false}'))
        self.assertFalse(is_joined('Потенциальные клиенты','not json'))

    def test_monitor_policy_enables_only_joined_chats(self):
        # A live public group the service account does not follow is never read, so it is disabled.
        self.add('live_unjoined')
        self.add('joined_channel',group='Требуют проверки',metadata=json.dumps({'joined':True,'kind':'channel'}),
                 enabled=0,members=1000,online=None)
        self.add('joined_group',group='joined',segment='joined',online=0,members=3)
        self.add('joined_segment',segment='joined',enabled=0)
        self.add('joined_inactive',group='joined',status='inactive')
        self.add('old_off',enabled=0,status='inactive')
        self.assertEqual(apply_monitor_policy(self.db),3)
        enabled={row['username']:row['enabled'] for row in self.db.execute('SELECT username,enabled FROM telegram_sources')}
        self.assertEqual(enabled,{'live_unjoined':0,'joined_channel':1,'joined_group':1,'joined_segment':1,
                                  'joined_inactive':0,'old_off':0})
        self.assertEqual(self.row('live_unjoined')['check_reason'],NOT_JOINED_REASON)
        self.assertEqual(self.row('old_off')['check_reason'],'83 онлайн при проверке')
        self.assertEqual(read_count(self.db),3)
        self.assertEqual(apply_monitor_policy(self.db),3)  # stable on re-run

    @patch('leadgen.source_registry.time.sleep')
    @patch('leadgen.source_registry.inspect_public_page')
    def test_verify_sources_does_not_enable_unjoined_live_pages(self,inspect,_sleep):
        inspect.return_value={'status':'active','reason':'83 онлайн при проверке','title':'Чат','members':1200,
                              'online':83,'last_message_at':None}
        self.add('pending_unjoined',status='pending',enabled=0)
        self.add('pending_joined',status='pending',enabled=0,metadata=json.dumps({'joined':True,'kind':'group'}))
        result=verify_pending(self.db,workers=1)
        self.assertEqual(result['enabled'],1)
        self.assertEqual((self.row('pending_unjoined')['enabled'],self.row('pending_unjoined')['check_reason']),
                         (0,NOT_JOINED_REASON))
        self.assertEqual((self.row('pending_joined')['enabled'],self.row('pending_joined')['status']),(1,'active'))

    def test_reimport_keeps_joined_marker(self):
        self.add('good_chat',metadata=json.dumps({'joined':True,'kind':'channel'}))
        data={'selected':[{'sheet':'Требуют проверки','url':'https://t.me/good_chat','title':'Good','segment':'Бизнес',
                           'priority':1,'members':100,'risks':'новый риск'}]}
        path=Path(self.tmp.name)/'data.json';path.write_text(json.dumps(data))
        import_selection(self.db,path)
        metadata=json.loads(self.row('good_chat')['metadata'])
        self.assertEqual((metadata['joined'],metadata['kind'],metadata['risks']),(True,'channel','новый риск'))
        self.assertEqual(apply_monitor_policy(self.db),1)

if __name__=='__main__': unittest.main()
