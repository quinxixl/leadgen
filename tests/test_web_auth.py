import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from leadgen.database import db_open
from leadgen.rate_limit import RateLimited, hit, purge_expired
from leadgen.web_auth import begin_login, code_choices, login_record, approve_login, consume_login


class WebLogin(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=db_open(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_browser_bound_single_use(self):
        token,browser,code=begin_login(self.db)
        self.assertEqual(len(code),6)
        self.assertIsNone(consume_login(self.db,token,browser))
        approve_login(self.db,token,12,code)
        with self.assertRaises(ValueError):consume_login(self.db,token,'another-browser')
        self.assertEqual(consume_login(self.db,token,browser),12)
        with self.assertRaises(ValueError):consume_login(self.db,token,browser)
    def test_approval_cannot_be_reassigned(self):
        token,_,code=begin_login(self.db);approve_login(self.db,token,12,code)
        with self.assertRaises(ValueError):approve_login(self.db,token,13,code)
    def test_wrong_code_burns_request(self):
        token,browser,code=begin_login(self.db)
        wrong=next(choice for choice in code_choices(login_record(self.db,token)) if choice!=code)
        with self.assertRaises(ValueError):approve_login(self.db,token,12,wrong)
        with self.assertRaises(ValueError):approve_login(self.db,token,12,code)
        with self.assertRaises(ValueError):consume_login(self.db,token,browser)
    def test_choices_contain_code_once(self):
        token,_,code=begin_login(self.db,'Chrome · macOS')
        record=login_record(self.db,token)
        choices=code_choices(record)
        self.assertEqual(len(set(choices)),4);self.assertIn(code,choices)
        self.assertEqual(record['requester'],'Chrome · macOS')
    def test_expired(self):
        with patch('leadgen.web_auth.time.time',return_value=100):token,_,_=begin_login(self.db)
        with patch('leadgen.web_auth.time.time',return_value=401):
            with self.assertRaises(ValueError):login_record(self.db,token)


class RateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=db_open(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_window_blocks_then_resets(self):
        with patch('leadgen.rate_limit.time.time',return_value=1000):
            for _ in range(3):hit(self.db,'x','1.2.3.4',3,60)
            with self.assertRaises(RateLimited):hit(self.db,'x','1.2.3.4',3,60)
            hit(self.db,'x','5.6.7.8',3,60)
        with patch('leadgen.rate_limit.time.time',return_value=1061):
            hit(self.db,'x','1.2.3.4',3,60)
    def test_purge_removes_only_stale(self):
        with patch('leadgen.rate_limit.time.time',return_value=1000):hit(self.db,'x','a',3,60)
        with patch('leadgen.rate_limit.time.time',return_value=2000):
            hit(self.db,'x','b',3,60);purge_expired(self.db)
        keys=[r['key'] for r in self.db.execute("SELECT key FROM settings WHERE key LIKE 'rl:%'")]
        self.assertEqual(len(keys),1)
