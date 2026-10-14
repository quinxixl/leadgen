import os
import unittest
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen.integrations import deliver_webhooks, validate_webhook_url
from leadgen.telegram_accounts import SessionCipher


def resolver_for(address):
    return lambda *args,**kwargs:[(2,1,6,'',(address,443))]


class WebhookSafety(unittest.TestCase):
    def test_accepts_public_https(self):
        self.assertEqual(validate_webhook_url('https://example.com/events?source=leadfinder',
            resolver_for('93.184.216.34')),'https://example.com/events?source=leadfinder')

    def test_rejects_private_hosts_credentials_ports_and_redirect_fragments(self):
        for url,resolver in [
            ('http://example.com',resolver_for('93.184.216.34')),
            ('https://user:pass@example.com',resolver_for('93.184.216.34')),
            ('https://example.com:8443',resolver_for('93.184.216.34')),
            ('https://example.com/hook#fragment',resolver_for('93.184.216.34')),
            ('https://localhost/hook',resolver_for('127.0.0.1')),
            ('https://metadata/hook',resolver_for('169.254.169.254'))]:
            with self.subTest(url=url),self.assertRaises(ValueError):validate_webhook_url(url,resolver)


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
@patch.dict(os.environ,{'INTEGRATIONS_ENABLED':'1'})
class WebhookSecrets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        self.key=self.app.config['TELEGRAM_CIPHER_KEY'];self.hooks={}
        with self.db:
            self.db.execute('DELETE FROM webhook_outbox')
            for name,secret in (('broken','not-a-fernet-token'),('healthy',SessionCipher(self.key).encrypt({'secret':'whsec_ok'}))):
                hook=self.db.execute('''INSERT INTO webhooks(workspace_id,name,url,secret_cipher,created_by)
                    VALUES(?,?,?,?,?) RETURNING id''',(self.ws,name,'https://example.com/'+name,secret,self.u)).fetchone()['id']
                self.db.execute('''INSERT INTO webhook_outbox(webhook_id,event_type,event_key,payload)
                    VALUES(?,'lead.updated',?,'{"event":"lead.updated"}'::jsonb)''',(hook,name))
                self.hooks[name]=hook

    def status(self,name):
        return self.db.execute('SELECT status,last_error FROM webhook_outbox WHERE webhook_id=?',(self.hooks[name],)).fetchone()

    def test_undecryptable_secret_marks_event_dead_and_others_still_go_out(self):
        calls=[]
        def sender(url,body,headers,resolver):calls.append(url)
        self.assertEqual(deliver_webhooks(self.db,self.key,sender,self.app.config['WEBHOOK_RESOLVER']),1)
        self.assertEqual(calls,['https://example.com/healthy'])
        broken=self.status('broken')
        self.assertEqual(broken['status'],'dead');self.assertIn('Секрет webhook',broken['last_error'])
        self.assertEqual(self.status('healthy')['status'],'sent')
        self.assertEqual(deliver_webhooks(self.db,self.key,sender,self.app.config['WEBHOOK_RESOLVER']),0)
        self.assertEqual(len(calls),1)

    def test_missing_server_key_leaves_queue_untouched(self):
        self.assertEqual(deliver_webhooks(self.db,'',lambda *args:None),0)
        self.assertEqual({self.status(name)['status'] for name in self.hooks},{'pending'})
