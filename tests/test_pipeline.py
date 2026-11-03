import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from storage import connect, initialize, now
import worker
import telegram_notifier


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = connect(self.root / 'board.sqlite')
        initialize(self.c)
        self.workspace = self.root / 'repo'
        self.workspace.mkdir()
        self.cfg = {'projects': {'demo': str(self.workspace)}, 'timeout_seconds': 3,
                    'commands': {r: [sys.executable, str(Path(__file__).with_name('fixture_agent.py')), r]
                                 for r in worker.ROLES}}
        self.env = patch.dict(os.environ, {'LLMBAN_TELEGRAM_ENABLED': '0'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.c.close()
        self.tmp.cleanup()

    def card(self, column='llm plan', project='demo', position=0):
        with self.c:
            cur = self.c.execute('''INSERT INTO cards(title,description,column_name,project,position,created_at,updated_at)
                VALUES('Make greeting','Write greeting.txt containing hello',?,?,?,?,?)''',
                (column, project, position, now(), now()))
        return cur.lastrowid

    def titles_in(self, column):
        return [r[0] for r in self.c.execute('SELECT id FROM cards WHERE column_name=? ORDER BY position,id', (column,))]

    def test_worker_moves_land_at_bottom_of_destination(self):
        waiting = [self.card('man plan', position=p) for p in (1, 2)]
        card = self.card('llm plan', position=1)
        self.assertTrue(worker.process_once(self.c, self.cfg))
        self.assertEqual(self.titles_in('man plan'), waiting + [card])
        self.assertEqual(self.get(card)['position'], 3)

    def test_cli_create_appends_to_end_of_column(self):
        existing = self.card('man backlog', position=1)
        env = dict(os.environ, LLMBAN_DB=str(self.root / 'board.sqlite'))
        out = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'bin' / 'board.py'),
                              'create', '--title', 'CLI card', '--column', 'man backlog'],
                             env=env, capture_output=True, text=True, check=True).stdout
        created = json.loads(out)['id']
        self.assertEqual(self.titles_in('man backlog'), [existing, created])
        self.assertEqual(self.get(created)['position'], 2)

    def test_worker_claims_cards_in_manual_order(self):
        later = self.card('llm todo', position=2)
        first = self.card('llm todo', position=1)
        run = worker.claim(self.c, self.cfg)
        self.assertEqual(run['card_id'], first)
        self.assertEqual(self.get(later)['column_name'], 'llm todo')

    def get(self, card_id):
        return self.c.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()

    def test_full_pipeline_changes_workspace_and_preserves_human_gates(self):
        card = self.card()
        self.assertTrue(worker.process_once(self.c, self.cfg))
        self.assertEqual(self.get(card)['column_name'], 'man plan')
        self.assertFalse(worker.process_once(self.c, self.cfg))
        with self.c:
            self.c.execute("UPDATE cards SET column_name='llm todo' WHERE id=?", (card,))
        worker.process_once(self.c, self.cfg)
        self.assertEqual(self.get(card)['column_name'], 'ready-to-test')
        self.assertEqual((self.workspace / 'greeting.txt').read_text(), 'hello')
        worker.process_once(self.c, self.cfg)
        self.assertEqual(self.get(card)['column_name'], 'man test')
        self.assertFalse(worker.process_once(self.c, self.cfg))
        self.assertEqual(self.c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT count(*) FROM agent_runs WHERE state='succeeded'").fetchone()[0], 3)

    def test_invalid_json_does_not_requeue_or_destroy_original_task(self):
        card = self.card()
        self.cfg['commands']['planner'] = [sys.executable, '-c', 'print("not JSON")']
        worker.process_once(self.c, self.cfg)
        self.assertEqual(self.get(card)['column_name'], 'man backlog')
        self.assertTrue(self.get(card)['description'].startswith('Write greeting.txt'))
        self.assertFalse(worker.process_once(self.c, self.cfg))

    def test_unknown_project_cannot_select_arbitrary_cwd(self):
        card = self.card('llm todo', '../../elsewhere')
        worker.process_once(self.c, self.cfg)
        self.assertEqual(self.get(card)['column_name'], 'man plan')
        self.assertFalse((self.workspace / 'greeting.txt').exists())

    def test_active_run_blocks_second_connection_until_explicit_recovery(self):
        card = self.card('llm todo')
        first = worker.claim(self.c, self.cfg)
        other = connect(self.root / 'board.sqlite')
        try:
            self.assertIsNone(worker.claim(other, self.cfg))
        finally:
            other.close()
        worker.recover(self.c)
        self.assertEqual(self.get(card)['column_name'], 'man plan')
        self.assertFalse(worker.process_once(self.c, self.cfg))

    def test_human_edits_are_not_overwritten(self):
        card = self.card('llm todo')
        run = worker.claim(self.c, self.cfg)
        with self.c:
            self.c.execute("UPDATE cards SET description='Human correction' WHERE id=?", (card,))
        worker.finish(self.c, run, data={'success': True, 'note': 'ok', 'report': 'verified'})
        self.assertEqual(self.get(card)['description'], 'Human correction')
        self.assertEqual(self.c.execute('SELECT state FROM agent_runs').fetchone()[0], 'superseded')

    def test_failed_test_returns_to_human_planning(self):
        card = self.card('llm test')
        worker.process_once(self.c, self.cfg)
        self.assertEqual(self.get(card)['column_name'], 'man plan')
        self.assertIn('missing greeting', self.get(card)['description'])

    def test_timeout_fails_once_and_terminates_process(self):
        card = self.card('llm todo')
        self.cfg['timeout_seconds'] = 1
        self.cfg['commands']['doer'] = [sys.executable, '-c', 'import time; time.sleep(30)']
        start = time.monotonic()
        worker.process_once(self.c, self.cfg)
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(self.get(card)['column_name'], 'man plan')
        self.assertIn('TimeoutExpired', self.get(card)['description'])

    def test_result_boolean_is_strict(self):
        with self.assertRaises(ValueError):
            worker.parse_result('{"note":"x","report":"x","success":"true"}', 'tester')

    def test_telegram_retry_does_not_lose_event(self):
        card = self.card('llm todo')
        with patch.dict(os.environ, {'LLMBAN_TELEGRAM_ENABLED': '1'}):
            worker.process_once(self.c, self.cfg)
        def fail(_):
            raise RuntimeError('secret-token-should-not-be-logged')
        with self.assertLogs('llmban.telegram', level='WARNING') as logs:
            telegram_notifier.deliver_once(self.c, fail)
        self.assertNotIn('secret-token', ''.join(logs.output))
        row = self.c.execute('SELECT * FROM notification_outbox').fetchone()
        self.assertIsNone(row['sent_at'])
        self.assertEqual(row['attempts'], 1)
        with self.c:
            self.c.execute('UPDATE notification_outbox SET next_attempt=0')
        messages = []
        telegram_notifier.deliver_once(self.c, messages.append)
        self.assertIn('doer', messages[0])
        self.assertNotIn('Write greeting', messages[0])
        self.assertIsNotNone(self.c.execute('SELECT sent_at FROM notification_outbox').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
