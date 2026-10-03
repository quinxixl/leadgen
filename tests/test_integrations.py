import unittest

from leadgen.integrations import validate_webhook_url


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
