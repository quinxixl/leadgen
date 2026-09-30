import unittest
from leadgen.web import create_app


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
