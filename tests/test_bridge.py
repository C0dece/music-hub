"""Мост для расширения: пускает только своё расширение и только свои действия.

Тест поднимает настоящий сервер на 127.0.0.1 и стучится к нему по-настоящему -
проверять такое разбором кода бессмысленно.
"""
import http.client
import json
import unittest

from app.core import bridge as bridge_mod

from .qt_app import qt_app

EXT_ORIGIN = 'chrome-extension://abcdefghijklmnopabcdefghijklmnop'
YT = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'


class CheckUrlTests(unittest.TestCase):
    def test_allowed(self):
        for url in (YT, 'https://youtu.be/abc', 'https://vk.com/audio1_2',
                    'http://m.youtube.com/watch?v=abc'):
            self.assertEqual(bridge_mod.check_url(url), url, url)

    def test_rejected(self):
        for url in ('file:///C:/Windows/System32/cmd.exe', 'data:text/html,<b>',
                    'javascript:alert(1)', 'https://evil.example/watch?v=1',
                    'ftp://youtube.com/x', '', None, 12345,
                    'https://youtube.com.evil.example/x', 'x' * 3000):
            self.assertEqual(bridge_mod.check_url(url), '', repr(url)[:40])


class BridgeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        self.bridge = bridge_mod.BridgeServer()
        self.token = bridge_mod.new_token()
        self.port = self.bridge.start(48311, self.token)
        self.assertTrue(self.port, 'мост не поднялся')
        self.bridge.set_status({'playing': False, 'track': None})
        self.received = []
        self.bridge.command.connect(lambda a, p: self.received.append((a, p)))

    def tearDown(self):
        self.bridge.stop()
        self.assertFalse(self.bridge.running)

    def request(self, method, path, body=None, token=None, origin=EXT_ORIGIN):
        headers = {}
        if token is not None:
            headers['X-Auth-Token'] = token
        if origin is not None:
            headers['Origin'] = origin
        payload = None
        if body is not None:
            payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    # ---------- доступ ----------
    def test_status_ok(self):
        status, body, _ = self.request('GET', '/status', token=self.token)
        self.assertEqual(status, 200)
        self.assertIn('playing', json.loads(body))

    def test_no_token_rejected(self):
        self.assertEqual(self.request('GET', '/status')[0], 401)

    def test_wrong_token_rejected(self):
        self.assertEqual(self.request('GET', '/status', token='wrong-token')[0], 401)

    def test_web_page_origin_rejected(self):
        """Любой сайт мог бы дёргать localhost - поэтому чужой Origin отбиваем."""
        status, _, _ = self.request('GET', '/status', token=self.token,
                                    origin='https://evil.example')
        self.assertEqual(status, 403)

    def test_unknown_path(self):
        self.assertEqual(self.request('GET', '/secret-path', token=self.token)[0], 404)

    def test_cors_never_wildcard(self):
        _, _, headers = self.request('GET', '/status', token=self.token)
        allow = headers.get('Access-Control-Allow-Origin', '')
        self.assertNotEqual(allow, '*')
        self.assertEqual(allow, EXT_ORIGIN)

    # ---------- команды ----------
    def test_known_action_accepted(self):
        status, _, _ = self.request('POST', '/command',
                                    {'action': 'play', 'url': YT, 'title': 'Test'},
                                    token=self.token)
        self.assertEqual(status, 200)
        self.app.processEvents()
        self.assertEqual(self.received[0][0], 'play')
        self.assertEqual(self.received[0][1]['url'], YT)

    def test_unknown_action_rejected(self):
        for action in ('execute', 'eval', 'download_file', '', None, 42):
            status, _, _ = self.request('POST', '/command',
                                        {'action': action, 'url': YT},
                                        token=self.token)
            self.assertEqual(status, 400, repr(action))
        self.app.processEvents()
        self.assertEqual(self.received, [])

    def test_local_path_rejected(self):
        for url in ('file:///C:/Users/me/secret.txt', 'C:\\Users\\me\\secret.txt',
                    '/etc/passwd', 'https://evil.example/song'):
            status, _, _ = self.request('POST', '/command',
                                        {'action': 'play', 'url': url},
                                        token=self.token)
            self.assertEqual(status, 400, url)
        self.app.processEvents()
        self.assertEqual(self.received, [])

    def test_body_limit(self):
        big = json.dumps({'action': 'play', 'url': YT, 'title': 'я' * 9000})
        self.assertEqual(self.request('POST', '/command', big, token=self.token)[0], 413)

    def test_broken_json(self):
        self.assertEqual(self.request('POST', '/command', '{not json'.encode(),
                                      token=self.token)[0], 400)

    def test_command_needs_token(self):
        status, _, _ = self.request('POST', '/command', {'action': 'play', 'url': YT})
        self.assertEqual(status, 401)
        self.app.processEvents()
        self.assertEqual(self.received, [])

    def test_title_is_truncated(self):
        self.request('POST', '/command',
                     {'action': 'enqueue', 'url': YT, 'title': 'д' * 1000},
                     token=self.token)
        self.app.processEvents()
        self.assertLessEqual(len(self.received[0][1]['title']), 300)


if __name__ == '__main__':
    unittest.main()
