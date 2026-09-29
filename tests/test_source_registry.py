import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from leadgen.app import db_open
from leadgen.source_registry import import_selection, inspect_public_page, monitor_allowed


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

    def test_monitor_policy_allows_read_only_chat_but_not_consumer_audience(self):
        self.assertTrue(monitor_allowed('Требуют проверки',json.dumps({'risks':'По источнику писать нельзя'})))
        self.assertFalse(monitor_allowed('Требуют проверки',json.dumps({'risks':'Возможна потребительская аудитория вместо специалистов'})))

if __name__=='__main__': unittest.main()
