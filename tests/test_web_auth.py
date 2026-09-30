import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from leadgen.database import db_open
from leadgen.web_auth import begin_login, login_record, approve_login, consume_login


class WebLogin(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=db_open(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_browser_bound_single_use(self):
        token,browser,code=begin_login(self.db)
        self.assertEqual(len(code),6)
        self.assertIsNone(consume_login(self.db,token,browser))
        approve_login(self.db,token,12)
        with self.assertRaises(ValueError):consume_login(self.db,token,'another-browser')
        self.assertEqual(consume_login(self.db,token,browser),12)
        with self.assertRaises(ValueError):consume_login(self.db,token,browser)
    def test_approval_cannot_be_reassigned(self):
        token,_,_=begin_login(self.db);approve_login(self.db,token,12)
        with self.assertRaises(ValueError):approve_login(self.db,token,13)
    def test_expired(self):
        with patch('leadgen.web_auth.time.time',return_value=100):token,_,_=begin_login(self.db)
        with patch('leadgen.web_auth.time.time',return_value=401):
            with self.assertRaises(ValueError):login_record(self.db,token)
