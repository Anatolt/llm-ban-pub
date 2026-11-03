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


    def create(self, token, title, column, project):
        with self.request(data={'csrf': token, 'action': 'create', 'title': title,
                                'column_name': column, 'project': project}):
            pass
        return [c for c in self.cards(project) if c['title'] == title][0]['id']

    def cards(self, project=None, column=None):
        with self.request('/api/cards' + (f'?project={project}' if project else '')) as r:
            cards = json.load(r)['cards']
        return [c for c in cards if column is None or c['column_name'] == column]

    def move(self, token, card_id, column, before=None):
        data = {'csrf': token, 'action': 'move', 'id': card_id, 'column_name': column}
        if before is not None:
            data['before_id'] = before
        with self.request(data=data) as r:
            return r.status

    def move_error(self, token, card_id, column, before=None):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.move(token, card_id, column, before)
        ctx.exception.close()
        return ctx.exception.code

    def titles(self, project=None, column=None):
        return [c['title'] for c in self.cards(project, column)]

    def test_reorder_within_column_and_persist(self):
        token = self.token()
        ids = {t: self.create(token, t, 'preflight', 'order-same') for t in ['A', 'B', 'C']}
        self.assertEqual(self.titles('order-same', 'preflight'), ['A', 'B', 'C'])
        self.move(token, ids['C'], 'preflight', before=ids['A'])
        self.assertEqual(self.titles('order-same', 'preflight'), ['C', 'A', 'B'])
        self.move(token, ids['C'], 'preflight')
        self.assertEqual(self.titles('order-same', 'preflight'), ['A', 'B', 'C'])
        self.move(token, ids['A'], 'preflight', before=ids['C'])
        self.assertEqual(self.titles('order-same', 'preflight'), ['B', 'A', 'C'])
        db = sqlite3.connect(Path(self.tmp.name) / 'board.sqlite')
        try:
            stored = db.execute("SELECT title FROM cards WHERE project='order-same' ORDER BY position, id").fetchall()
        finally:
            db.close()
        self.assertEqual([row[0] for row in stored], ['B', 'A', 'C'])
        positions = [c['position'] for c in self.cards('order-same', 'preflight')]
        self.assertEqual(positions, [1, 2, 3])

    def test_cross_column_drop_takes_position_and_renumbers_source(self):
        token = self.token()
        src = {t: self.create(token, t, 'llm planing', 'order-cross') for t in ['S1', 'S2', 'S3']}
        dst = {t: self.create(token, t, 'llm test', 'order-cross') for t in ['D1', 'D2']}
        self.move(token, src['S2'], 'llm test', before=dst['D2'])
        self.assertEqual(self.titles('order-cross', 'llm test'), ['D1', 'S2', 'D2'])
        self.assertEqual(self.titles('order-cross', 'llm planing'), ['S1', 'S3'])
        self.assertEqual([c['position'] for c in self.cards('order-cross', 'llm planing')], [1, 2])
        self.assertEqual([c['position'] for c in self.cards('order-cross', 'llm test')], [1, 2, 3])
        self.move(token, src['S1'], 'llm test')
        self.assertEqual(self.titles('order-cross', 'llm test'), ['D1', 'S2', 'D2', 'S1'])

    def test_filtered_board_keeps_hidden_card_positions(self):
        token = self.token()
        ids = {}
        for title, project in [('A1', 'vis'), ('B1', 'hidden'), ('A2', 'vis'), ('B2', 'hidden'), ('A3', 'vis')]:
            ids[title] = self.create(token, title, 'llm testing', project)
        self.move(token, ids['A3'], 'llm testing', before=ids['A2'])
        self.assertEqual(self.titles('vis', 'llm testing'), ['A1', 'A3', 'A2'])
        self.assertEqual(self.titles('hidden', 'llm testing'), ['B1', 'B2'])
        self.assertEqual(self.titles(None, 'llm testing'), ['A1', 'B1', 'A3', 'A2', 'B2'])

    def test_invalid_or_stale_reorder_is_rejected_without_losing_cards(self):
        token = self.token()
        a = self.create(token, 'keep-a', 'man test', 'order-bad')
        b = self.create(token, 'keep-b', 'man done', 'order-bad')
        self.assertEqual(self.move_error(token, a, 'man test', before=999999), 409)
        self.assertEqual(self.move_error(token, a, 'man test', before=b), 409)
        self.assertEqual(self.move_error(token, a, 'man test', before=a), 422)
        self.assertEqual(self.move_error(token, 0, 'man test'), 422)
        self.assertEqual(self.move_error(token, a, 'man test', before=0), 422)
        self.assertEqual(self.move_error(token, 999999, 'man test'), 404)
        self.assertEqual(self.titles('order-bad', 'man test'), ['keep-a'])
        self.assertEqual(self.titles('order-bad', 'man done'), ['keep-b'])

    def test_json_move_reports_position_and_errors(self):
        token = self.token()
        a = self.create(token, 'j1', 'ready-to-test', 'order-json')
        b = self.create(token, 'j2', 'ready-to-test', 'order-json')
        headers = {'Authorization': self.auth, 'Accept': 'application/json', 'X-Requested-With': 'fetch'}
        body = urllib.parse.urlencode({'csrf': token, 'action': 'move', 'id': b, 'column_name': 'ready-to-test',
                                       'before_id': a}).encode()
        with self.opener.open(urllib.request.Request(self.url + '/', data=body, headers=headers), timeout=5) as r:
            self.assertEqual(json.load(r), {'ok': True, 'id': b, 'column_name': 'ready-to-test', 'position': 1})
        body = urllib.parse.urlencode({'csrf': token, 'action': 'move', 'id': b, 'column_name': 'ready-to-test',
                                       'before_id': 999999}).encode()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.opener.open(urllib.request.Request(self.url + '/', data=body, headers=headers), timeout=5)
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(json.load(ctx.exception), {'ok': False, 'error': 'Stale position'})
        ctx.exception.close()

    def test_edit_form_column_change_appends_to_end(self):
        token = self.token()
        first = self.create(token, 'e-first', 'llm doing', 'order-edit')
        self.create(token, 'e-second', 'llm doing', 'order-edit')
        moved = self.create(token, 'e-moved', 'man plan', 'order-edit')
        with self.request(data={'csrf': token, 'action': 'update', 'id': moved, 'title': 'e-moved',
                                'description': '', 'column_name': 'llm doing', 'project': 'order-edit'}):
            pass
        self.assertEqual(self.titles('order-edit', 'llm doing'), ['e-first', 'e-second', 'e-moved'])
        with self.request(data={'csrf': token, 'action': 'update', 'id': first, 'title': 'e-first renamed',
                                'description': '', 'column_name': 'llm doing', 'project': 'order-edit'}):
            pass
        self.assertEqual(self.titles('order-edit', 'llm doing'), ['e-first renamed', 'e-second', 'e-moved'])

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
