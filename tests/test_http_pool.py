"""Shared keep-alive HTTP client (adapters.fetch): one pool and one SSL context, urllib-compatible errors."""
import json
import os
import socket
import ssl
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

try:
    import urllib3
except ImportError:
    urllib3 = None

from leadgen import adapters
from leadgen.app import bot_api_timeout, telegram


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'  # keep-alive

    def do_GET(self):
        self.server.connections.add(self.client_address)
        status = 403 if self.path == '/forbidden' else 200
        body = b'x' * (adapters.MAX_BODY + 10) if self.path == '/big' else json.dumps({'ok': True, 'result': self.path}).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(length) or b'{}')
        self.server.connections.add(self.client_address)
        body = json.dumps({'ok': True, 'result': payload}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(urllib3, 'urllib3 is not installed')
class SharedPool(unittest.TestCase):
    def setUp(self):
        adapters._http_state = None
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.connections = set()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        adapters._http_state = None

    def test_many_calls_reuse_one_pool_one_ssl_context_and_one_connection(self):
        with patch('leadgen.adapters.ssl.create_default_context', wraps=ssl.create_default_context) as contexts:
            for index in range(20):
                body = adapters.fetch(self.base + '/bot/sendMessage', json.dumps({'n': index}).encode(),
                                      {'Content-Type': 'application/json'})
                self.assertEqual(json.loads(body)['result'], {'n': index})
            pool = adapters.http_pool()
        self.assertEqual(contexts.call_count, 1)        # one SSL context for the process, not one per call
        self.assertIs(adapters.http_pool(), pool)
        self.assertEqual(len(self.server.connections), 1)  # keep-alive: one TCP connection served all 20 calls

    def test_pool_is_rebuilt_after_fork_or_new_cafile(self):
        pool = adapters.http_pool()
        with patch('leadgen.adapters.os.getpid', return_value=os.getpid() + 1):
            self.assertIsNot(adapters.http_pool(), pool)

    def test_errors_keep_urllib_types(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            adapters.fetch(self.base + '/forbidden')
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(ValueError):
            adapters.fetch(self.base + '/big')
        self.assertIn(b'/after', adapters.fetch(self.base + '/after'))  # the oversized response did not poison the pool
        free = socket.socket();free.bind(('127.0.0.1', 0));port = free.getsockname()[1];free.close()
        with self.assertRaises(urllib.error.URLError) as caught:
            adapters.fetch(f'http://127.0.0.1:{port}/')
        self.assertIsInstance(caught.exception.reason, ConnectionError)

    def test_read_timeout_is_a_timeout_error(self):
        silent = socket.socket();silent.bind(('127.0.0.1', 0));silent.listen(1)
        try:
            with self.assertRaises(TimeoutError):
                adapters.fetch(f'http://127.0.0.1:{silent.getsockname()[1]}/', timeout=0.2)
        finally:
            silent.close()

    def test_no_retries_and_redirects_followed(self):
        retry = adapters._no_retries()
        self.assertEqual((retry.connect, retry.read, retry.other, retry.redirect), (0, 0, 0, 10))


class BotApiTimeout(unittest.TestCase):
    def test_http_timeout_outlasts_the_long_poll(self):
        self.assertGreater(bot_api_timeout('getUpdates', {'timeout': 25}), 25)
        self.assertEqual(bot_api_timeout('sendMessage', {}), adapters.FETCH_TIMEOUT)

    def test_telegram_passes_the_timeout_to_the_shared_client(self):
        token = '123:' + 'a' * 30
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': token}), \
                patch('leadgen.app.fetch', return_value=b'{"ok":true,"result":[]}') as fetch:
            self.assertEqual(telegram('getUpdates', {'timeout': 25}), [])
        self.assertEqual(fetch.call_args.kwargs['timeout'], 35)


if __name__ == '__main__':
    unittest.main()
