import unittest
import os
from unittest.mock import patch
from leadgen.web import create_app, is_platform_admin, platform_admin_ids


class WebPublic(unittest.TestCase):
    def setUp(self):
        self.app=create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'BOT_USERNAME':'test_bot',
                            'SESSION_COOKIE_SECURE':False})
        self.client=self.app.test_client()
    def test_public_routes_and_auth_boundary(self):
        for path in ('/','/login','/healthz','/web_static/app.css'):
            with self.client.get(path) as response:self.assertEqual(response.status_code,200,path)
        for path in ('/app','/app/leads','/app/settings','/app/sources','/admin'):
            self.assertEqual(self.client.get(path).status_code,302,path)
    def test_csrf_required(self):
        self.client.get('/login')
        self.assertEqual(self.client.post('/login',data={'action':'begin'}).status_code,400)
        self.assertEqual(self.client.post('/logout').status_code,400)
    def test_headers_and_russian_markup(self):
        r=self.client.get('/')
        self.assertIn('frame-ancestors',r.headers['Content-Security-Policy'])
        self.assertIn('lang="ru"',r.text)
        self.assertIn('HttpOnly',r.headers['Set-Cookie'])
    def test_invalid_host(self):
        self.assertEqual(self.client.get('/',base_url='http://attacker.example').status_code,400)
    def test_platform_admin_ids_accept_multiple_values_and_spaces(self):
        with patch.dict(os.environ,{'WEB_ADMIN_TELEGRAM_IDS':'123, 5947200567, '}):
            self.assertEqual(platform_admin_ids(),{'123','5947200567'})
            self.assertTrue(is_platform_admin({'telegram_user_id':5947200567}))
            self.assertFalse(is_platform_admin({'telegram_user_id':999}))


class Helpers(unittest.TestCase):
    def test_like_pattern_escapes_wildcards(self):
        from leadgen.web import like_pattern
        self.assertEqual(like_pattern('50%_'),'%50\\%\\_%')
    def test_subscription_rules(self):
        from datetime import datetime,timedelta,timezone
        from leadgen.billing import subscription_active
        now=datetime.now(timezone.utc)
        self.assertTrue(subscription_active({'status':'stub_active','ends_at':None}))
        self.assertTrue(subscription_active({'status':'trial','ends_at':now+timedelta(days=1)}))
        self.assertFalse(subscription_active({'status':'trial','ends_at':now-timedelta(seconds=1)}))
        self.assertFalse(subscription_active({'status':'canceled','ends_at':None}))
        self.assertFalse(subscription_active(None))
    def test_mapped_loopback_webhook_rejected(self):
        import socket
        from leadgen.integrations import resolve_public
        records=lambda *a,**k:[(socket.AF_INET6,socket.SOCK_STREAM,6,'',('::ffff:127.0.0.1',443,0,0))]
        with self.assertRaises(ValueError):resolve_public('evil.example',records)


class Metrika(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.env=patch.dict(os.environ,{'YANDEX_METRIKA_ID':'113384418'});self.env.start()
        self.client=create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'BOT_USERNAME':'test_bot',
                                'SESSION_COOKIE_SECURE':False}).test_client()
    def tearDown(self):self.env.stop()
    def test_counter_on_public_pages_with_csp(self):
        r=self.client.get('/')
        self.assertIn('data-id="113384418"',r.text);self.assertIn('mc.yandex.ru/watch/113384418',r.text)
        csp=r.headers['Content-Security-Policy']
        self.assertIn("script-src 'self' https://mc.yandex.ru",csp);self.assertIn('metrika.yandex.ru',csp)
        self.assertNotIn('X-Frame-Options',r.headers)
    def test_no_counter_or_framing_in_cabinet_paths(self):
        r=self.client.get('/app')
        self.assertNotIn('mc.yandex.ru',r.headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Frame-Options'],'DENY')
    def test_disabled_without_id(self):
        with __import__('unittest.mock').mock.patch.dict(os.environ,{'YANDEX_METRIKA_ID':''}):
            r=self.client.get('/')
        self.assertNotIn('metrika.js',r.text);self.assertNotIn('mc.yandex',r.headers['Content-Security-Policy'])
