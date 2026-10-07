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
    def test_legal_pages_filled_with_requisites(self):
        for path in ('/terms','/privacy'):
            r=self.client.get(path)
            self.assertEqual(r.status_code,200,path)
            self.assertIn('Маленюк',r.text,path)
            self.assertNotIn('[',r.text.replace('[место хранения данных уточняется]',''),path)
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
    def test_canonical_and_social_tags(self):
        r=self.client.get('/pricing')
        self.assertIn('<link rel="canonical" href="http://localhost/pricing">',r.text)
        self.assertIn('property="og:url" content="http://localhost/pricing"',r.text)
        self.assertIn('name="twitter:card"',r.text)
        self.assertIn('/web_static/og.png',r.text)
    def test_structured_data_on_landing(self):
        r=self.client.get('/')
        self.assertIn('application/ld+json',r.text)
        self.assertIn('"@type":"Organization"',r.text)
        self.assertIn('"@type":"FAQPage"',r.text)
    def test_sitemap_and_robots(self):
        s=self.client.get('/sitemap.xml')
        self.assertEqual(s.status_code,200)
        self.assertIn('xml',s.headers['Content-Type'])
        self.assertIn('<loc>http://localhost/pricing</loc>',s.text)
        self.assertNotIn('/login',s.text)
        robots=self.client.get('/robots.txt')
        self.assertIn('Sitemap: http://localhost/sitemap.xml',robots.text)
    def test_static_og_image_served(self):
        with self.client.get('/web_static/og.png') as response:
            self.assertEqual(response.status_code,200)
    def test_favicon_ico_served_at_root(self):
        with self.client.get('/favicon.ico') as response:
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.headers['Content-Type'].startswith('image/'))
    def test_platform_admin_ids_accept_multiple_values_and_spaces(self):
        with patch.dict(os.environ,{'WEB_ADMIN_TELEGRAM_IDS':'123, 5947200567, '}):
            self.assertEqual(platform_admin_ids(),{'123','5947200567'})
            self.assertTrue(is_platform_admin({'telegram_user_id':5947200567}))
            self.assertFalse(is_platform_admin({'telegram_user_id':999}))


class MiniAppPublic(unittest.TestCase):
    def setUp(self):
        self.app=create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'BOT_USERNAME':'test_bot',
                            'SESSION_COOKIE_SECURE':False,'TELEGRAM_BOT_TOKEN':'123456:'+'A'*35})
        self.client=self.app.test_client()
    def test_tg_csp_allows_telegram_only_and_framing_by_telegram_web(self):
        r=self.client.get('/tg')
        self.assertEqual(r.status_code,200)
        csp=r.headers['Content-Security-Policy']
        self.assertIn("script-src 'self' https://telegram.org;",csp)
        self.assertIn('frame-ancestors https://web.telegram.org;',csp)
        self.assertNotIn('mc.yandex',csp)
        self.assertNotIn('X-Frame-Options',r.headers)
        self.assertIn('https://telegram.org/js/telegram-web-app.js',r.text)
        self.assertIn('/web_static/tg/tg.js',r.text)
        self.assertNotIn('metrika',r.text)
        self.assertNotIn('<script>',r.text)
    def test_cabinet_csp_unchanged(self):
        for path in ('/app','/login','/app/security'):
            r=self.client.get(path)
            csp=r.headers['Content-Security-Policy']
            self.assertIn("script-src 'self';",csp,path)
            self.assertNotIn('telegram.org',csp,path)
            self.assertIn("frame-ancestors 'none'",csp,path)
            self.assertEqual(r.headers['X-Frame-Options'],'DENY',path)
    def test_tg_cookie_is_separate_partitioned_and_cross_site(self):
        cookie=self.client.get('/tg').headers['Set-Cookie']
        for part in ('signalid_tg=','Secure','HttpOnly','Path=/tg','SameSite=None','Partitioned'):
            self.assertIn(part,cookie)
        main=self.app.test_client().get('/login').headers['Set-Cookie']
        self.assertIn('signalid_session=',main);self.assertIn('SameSite=Lax',main);self.assertNotIn('Partitioned',main)
    def test_cabinet_session_lasts_30_days(self):
        from datetime import timedelta
        self.assertEqual(self.app.permanent_session_lifetime,timedelta(days=30))
    def test_private_tg_pages_bootstrap_and_return(self):
        for path,target in (('/tg/feed','/tg/feed'),('/tg/leads/5','/tg/leads/5'),('/tg/billing','/tg/billing'),
                            ('/tg/start','/tg/start'),('/tg/settings','/tg/settings')):
            r=self.client.get(path)
            self.assertEqual(r.status_code,302,path)
            self.assertTrue(r.location.startswith('/tg?next='),r.location)
            page=self.client.get(r.location)
            self.assertIn(f'data-target="{target}"',page.text)
        self.assertEqual(self.client.post('/tg/handoff',data={}).status_code,400)
    def test_boot_never_targets_itself_or_other_sites(self):
        for value in ('/tg','/tg?x=1','https://evil.example/tg/feed','//evil.example','/tg/../app','/app','/tg/auth','/tgx'):
            r=self.client.get('/tg',query_string={'next':value})
            self.assertIn('data-target=""',r.text,value)
    def test_auth_rejects_cross_origin_and_bad_init_data(self):
        r=self.client.post('/tg/auth',data={'init_data':'x'},headers={'Origin':'https://evil.example'})
        self.assertEqual(r.status_code,403);self.assertFalse(r.json['ok'])
    def test_safe_next(self):
        from leadgen.web import safe_next
        self.assertEqual(safe_next('/app/billing','/app'),'/app/billing')
        self.assertEqual(safe_next('/app','/app'),'/app')
        self.assertEqual(safe_next('/app/leads?status=won','/app'),'/app/leads?status=won')
        for bad in ('//evil.example','https://evil.example/app','/application','/app/../admin','/app\\x','/tg/feed','',None,'/app//x','/app/<script>'):
            self.assertEqual(safe_next(bad,'/app'),'',bad)
    def test_login_status_without_request(self):
        self.assertEqual(self.client.get('/login/status').json,{'status':'none'})
    def test_login_page_polls_only_while_code_shown(self):
        self.assertNotIn('login.js',self.client.get('/login').text)
        with self.client.get('/web_static/login.js') as response:self.assertEqual(response.status_code,200)


class Helpers(unittest.TestCase):
    def test_chat_spheres_keep_all_matching_categories(self):
        from leadgen.web_telegram import chat_spheres
        self.assertEqual(chat_spheres('Фриланс: дизайн и монтаж'),
                         ['Фриланс и заказы','Дизайн','Видео и монтаж'])
        self.assertIn('Автоматизация и CRM',chat_spheres('Интеграции amoCRM и n8n'))
        self.assertIn('Юриспруденция',chat_spheres('Чат юристов и заказчиков'))
        self.assertEqual(chat_spheres('Общий чат соседей'),['Другое'])
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
        self.assertIn("script-src 'self' https://mc.yandex.ru",csp);self.assertIn("'unsafe-inline'",csp);self.assertIn('metrika.yandex.ru',csp)
        self.assertNotIn('X-Frame-Options',r.headers)
    def test_no_counter_or_framing_in_cabinet_paths(self):
        r=self.client.get('/app')
        self.assertNotIn('mc.yandex.ru',r.headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Frame-Options'],'DENY')
    def test_no_counter_on_login_page(self):
        r=self.client.get('/login')
        self.assertNotIn('metrika.js',r.text)
        csp=r.headers['Content-Security-Policy']
        self.assertNotIn('mc.yandex',csp);self.assertIn("frame-ancestors 'none'",csp)
    def test_disabled_without_id(self):
        with __import__('unittest.mock').mock.patch.dict(os.environ,{'YANDEX_METRIKA_ID':''}):
            r=self.client.get('/')
        self.assertNotIn('metrika.js',r.text);self.assertNotIn('mc.yandex',r.headers['Content-Security-Policy'])


class SiteVerification(unittest.TestCase):
    def _client(self):
        return create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'BOT_USERNAME':'test_bot',
                           'SESSION_COOKIE_SECURE':False}).test_client()
    def test_yandex_verification_present_when_set(self):
        from unittest.mock import patch
        with patch.dict(os.environ,{'YANDEX_VERIFICATION':'65d582a01b72026c'}):
            r=self._client().get('/')
        self.assertIn('name="yandex-verification"',r.text)
        self.assertIn('content="65d582a01b72026c"',r.text)
    def test_yandex_verification_absent_when_unset(self):
        from unittest.mock import patch
        with patch.dict(os.environ,{'YANDEX_VERIFICATION':''}):
            r=self._client().get('/')
        self.assertNotIn('name="yandex-verification"',r.text)


class Goals(unittest.TestCase):
    def test_goal_markers_present(self):
        from unittest.mock import patch
        with patch.dict(os.environ,{'YANDEX_METRIKA_ID':'1'}):
            client=create_app({'TESTING':True,'SECRET_KEY':'test-'*10,'BOT_USERNAME':'b','SESSION_COOKIE_SECURE':False}).test_client()
            self.assertIn('data-goal="cta_trial"',client.get('/').text)
            self.assertIn('data-goal-view="pricing_view"',client.get('/pricing').text)
            self.assertIn("style-src 'self';",client.get('/app').headers['Content-Security-Policy'])
