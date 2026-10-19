import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from leadgen.app import db_open
from leadgen.source_registry import (APPROVED_REASON, MODERATION_REASON, NOT_JOINED_REASON, REJECTED_REASON,
                                     apply_monitor_policy, approve_source, import_selection, inspect_public_page,
                                     is_joined, moderation_queue, read_count, reject_source, verify_pending)


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

    @patch('leadgen.source_registry.fetch',side_effect=ConnectionResetError('reset'))
    def test_dropped_connection_is_retried_later(self,fetch):
        # The keep-alive client reports a connection dropped mid-response as ConnectionError, as urllib did.
        self.assertEqual(inspect_public_page('business_chat'),{'status':'retry','reason':'ConnectionResetError'})

    def add(self,username,group='Потенциальные клиенты',segment='Бизнес',metadata='{}',status='active',
            enabled=1,members=100,online=5,reason='83 онлайн при проверке'):
        with self.db:
            self.db.execute('''INSERT INTO telegram_sources
                (username,url,title,segment,selection_group,members,online,status,enabled,check_reason,metadata)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (username,'https://t.me/'+username,username.title(),segment,group,members,online,status,enabled,
                 reason,metadata))

    def row(self,username):
        return self.db.execute('SELECT * FROM telegram_sources WHERE username=?',(username,)).fetchone()

    def test_joined_marker_sources(self):
        self.assertTrue(is_joined('joined','{}'));self.assertTrue(is_joined('x','{}','joined'))
        self.assertTrue(is_joined('x','{"joined":true}'));self.assertFalse(is_joined('x','{"joined":false}'))
        self.assertFalse(is_joined('Потенциальные клиенты','not json'))

    def test_monitor_policy_keeps_only_joined_chats_and_never_enables(self):
        # A live public group the service account does not follow is never read, so it is disabled.
        self.add('live_unjoined')
        self.add('joined_group',group='joined',segment='joined',online=0,members=3)
        self.add('joined_channel',group='Требуют проверки',metadata=json.dumps({'joined':True,'kind':'channel'}),
                 members=1000,online=None)
        self.add('joined_inactive',group='joined',status='inactive')
        self.add('old_off',enabled=0,status='inactive')
        # Disabled by an admin while joined: the policy leaves it off.
        self.add('admin_off',segment='joined',enabled=0,reason='отключён вручную')
        self.add('rejected_chat',group='joined',status='rejected',enabled=0,reason=REJECTED_REASON)
        # A catalog chat the registry disabled as unjoined, joined later: it goes to moderation, not on.
        self.add('catalog_joined',metadata=json.dumps({'joined':True,'kind':'group'}),enabled=0,reason=NOT_JOINED_REASON)
        self.assertEqual(apply_monitor_policy(self.db),2)
        rows={row['username']:(row['status'],row['enabled'])
              for row in self.db.execute('SELECT username,status,enabled FROM telegram_sources')}
        self.assertEqual(rows,{'live_unjoined':('active',0),'joined_group':('active',1),'joined_channel':('active',1),
                               'joined_inactive':('inactive',0),'old_off':('inactive',0),'admin_off':('active',0),
                               'rejected_chat':('rejected',0),'catalog_joined':('moderation',0)})
        self.assertEqual(self.row('live_unjoined')['check_reason'],NOT_JOINED_REASON)
        self.assertEqual(self.row('old_off')['check_reason'],'83 онлайн при проверке')
        self.assertEqual(self.row('admin_off')['check_reason'],'отключён вручную')
        self.assertEqual(self.row('rejected_chat')['check_reason'],REJECTED_REASON)
        self.assertEqual(self.row('catalog_joined')['check_reason'],MODERATION_REASON)
        self.assertEqual(read_count(self.db),2)
        self.assertEqual(apply_monitor_policy(self.db),2)  # stable on re-run
        self.assertEqual([r['username'] for r in moderation_queue(self.db)],['catalog_joined'])

    @patch('leadgen.source_registry.time.sleep')
    @patch('leadgen.source_registry.inspect_public_page')
    def test_verify_sources_sends_joined_catalog_chats_to_moderation(self,inspect,_sleep):
        inspect.return_value={'status':'active','reason':'83 онлайн при проверке','title':'Чат','members':1200,
                              'online':83,'last_message_at':None}
        joined=json.dumps({'joined':True,'kind':'group'})
        self.add('pending_unjoined',status='pending',enabled=0)
        self.add('pending_joined',status='pending',enabled=0,metadata=joined)
        self.add('retry_joined',status='retry',enabled=0,metadata=joined)
        self.add('rejected_chat',group='joined',status='rejected',enabled=0,reason=REJECTED_REASON)
        self.add('admin_off',status='active',enabled=0,metadata=joined,reason='отключён вручную')
        result=verify_pending(self.db,workers=1)
        self.assertEqual(result['checked'],3)  # rejected and admin-disabled rows are not re-checked
        self.assertEqual(result['enabled'],0)
        self.assertEqual(result['counts'],{'active':1,'moderation':2})
        self.assertEqual((self.row('pending_unjoined')['enabled'],self.row('pending_unjoined')['check_reason']),
                         (0,NOT_JOINED_REASON))
        for username in ('pending_joined','retry_joined'):
            row=self.row(username)
            self.assertEqual((row['status'],row['enabled'],row['check_reason']),('moderation',0,MODERATION_REASON))
        self.assertEqual((self.row('rejected_chat')['status'],self.row('rejected_chat')['enabled']),('rejected',0))
        self.assertEqual((self.row('admin_off')['status'],self.row('admin_off')['enabled'],
                          self.row('admin_off')['check_reason']),('active',0,'отключён вручную'))
        self.assertEqual(sorted(r['username'] for r in moderation_queue(self.db)),['pending_joined','retry_joined'])
        # The admin decides as for chats the reader registered on its own.
        self.assertTrue(approve_source(self.db,'pending_joined'))
        self.assertTrue(reject_source(self.db,'retry_joined'))
        self.assertEqual((self.row('pending_joined')['status'],self.row('pending_joined')['enabled'],
                          self.row('pending_joined')['check_reason']),('active',1,APPROVED_REASON))
        self.assertEqual(apply_monitor_policy(self.db),1)  # an approved catalog chat stays on
        self.assertEqual((self.row('retry_joined')['status'],self.row('retry_joined')['enabled']),('rejected',0))
        self.assertEqual(read_count(self.db),1)

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
