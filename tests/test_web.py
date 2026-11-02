"""Real HTTP tests against PHP with an empty, temporary SQLite database."""
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('php'), 'PHP not installed')
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        cls.url = f'http://127.0.0.1:{port}'
        env = dict(os.environ, LLMBAN_DB=str(Path(cls.tmp.name) / 'board.sqlite'),
                   LLMBAN_USERNAME='test', LLMBAN_PASSWORD='test-password-at-least-16', LLMBAN_BASE_PATH='')
        cls.log = tempfile.TemporaryFile()
        cls.proc = subprocess.Popen(['php', '-S', f'127.0.0.1:{port}', '-t', str(ROOT / 'public'),
                                     str(ROOT / 'public' / 'router.php')], env=env, stdout=cls.log, stderr=cls.log)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                if cls.proc.poll() is not None:
                    raise RuntimeError('PHP failed to start')
                time.sleep(.05)
        cls.auth = 'Basic ' + base64.b64encode(b'test:test-password-at-least-16').decode()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.log.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path='/', data=None, auth=True):
        headers = {'Authorization': self.auth} if auth else {}
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        return self.opener.open(urllib.request.Request(self.url + path, data=body, headers=headers), timeout=5)

    def token(self):
        with self.request('/?action=new') as r:
            return re.search('name="csrf" value="([a-f0-9]+)"', r.read().decode())[1]

    def test_authentication_required_for_html_and_api(self):
        for path in ['/', '/api/cards', '/api/work']:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request(path, auth=False)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

    def test_csrf_rejects_mutation_without_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request(data={'action': 'create', 'title': 'bad'})
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_create_move_edit_and_escape_html(self):
        token = self.token()
        with self.request(data={'csrf': token, 'action': 'create', 'title': '<script>alert(1)</script>',
                                'description': 'test', 'project': 'demo', 'column_name': 'man backlog'}):
            pass
        with self.request('/api/cards?project=demo') as r:
            card = json.load(r)['cards'][-1]
        with self.request('/') as r:
            html = r.read().decode()
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
            self.assertNotIn('<script>alert(1)</script>', html)
        with self.request(data={'csrf': token, 'action': 'move', 'id': card['id'], 'column_name': 'llm plan'}):
            pass
        with self.request('/api/cards?project=demo') as r:
            self.assertEqual(json.load(r)['cards'][-1]['column_name'], 'llm plan')

    def test_detail_indicator_follows_description(self):
        token = self.token()
        for title, description in [('detailed one', 'has text'), ('empty one', ''),
                                   ('blank one', ' \t\n ')]:
            with self.request(data={'csrf': token, 'action': 'create', 'title': title,
                                    'description': description, 'project': 'indicator',
                                    'column_name': 'man backlog'}):
                pass
        with self.request('/api/cards?project=indicator') as r:
            cards = {card['title']: card['id'] for card in json.load(r)['cards']}

        def shows_indicator(card_id):
            with self.request('/?project=indicator') as r:
                html = r.read().decode()
            block = re.search(rf'<article class="card" draggable="true" data-card-id="{card_id}">(.*?)</article>',
                              html, re.S)[1]
            return 'card-detailed' in block

        self.assertTrue(shows_indicator(cards['detailed one']))
        self.assertFalse(shows_indicator(cards['empty one']))
        self.assertFalse(shows_indicator(cards['blank one']))

        for title, description, card_id in [('empty one', 'now described', cards['empty one']),
                                            ('detailed one', '', cards['detailed one'])]:
            with self.request(data={'csrf': token, 'action': 'update', 'id': card_id, 'title': title,
                                    'description': description, 'project': 'indicator',
                                    'column_name': 'man backlog'}):
                pass
        self.assertTrue(shows_indicator(cards['empty one']))
        self.assertFalse(shows_indicator(cards['detailed one']))

    def test_unknown_column_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request(data={'csrf': self.token(), 'action': 'create', 'title': 'bad', 'column_name': 'unknown'})
        self.assertEqual(ctx.exception.code, 422)
        ctx.exception.close()

    def test_web_waits_for_concurrent_worker_write(self):
        self.token()  # initialize the schema before taking a write transaction
        c = sqlite3.connect(Path(self.tmp.name) / 'board.sqlite')
        try:
            c.execute('BEGIN IMMEDIATE')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: self.request('/api/cards'))
                time.sleep(.15)
                c.rollback()
                with future.result(timeout=5) as response:
                    self.assertIn('cards', json.load(response))
        finally:
            c.close()


if __name__ == '__main__':
    unittest.main()
