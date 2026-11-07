import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
from board import COLUMNS
from board_export import export_board
from storage import connect, initialize


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'board.sqlite'
        self.connection = connect(self.db)
        initialize(self.connection)
        self.addCleanup(self.connection.close)

    def card(self, title='Task', description='', project='demo', column='man backlog', position=0):
        with self.connection:
            return self.connection.execute(
                '''INSERT INTO cards(title,description,project,column_name,position,created_at,updated_at)
                   VALUES(?,?,?,?,?,'2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')''',
                (title, description, project, column, position)).lastrowid

    def cli(self, *args, db=None):
        return subprocess.run([sys.executable, str(ROOT / 'bin' / 'board.py'), 'export', *args],
                              env=dict(os.environ, LLMBAN_DB=str(db or self.db)),
                              capture_output=True)

    def test_stdout_is_deterministic_utf8_and_does_not_mutate_storage(self):
        self.card(title='Привет', description='First line\n\nSecond line')
        before = list(self.connection.iterdump())
        first = self.cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, b'')
        self.assertEqual(first.stdout, self.cli('--output', '-').stdout)
        text = first.stdout.decode('utf-8')
        self.assertIn('Привет', text)
        self.assertIn('First line\n\nSecond line', text)
        self.assertIn('Created: 2026', text)
        self.assertIn('Updated: 2026', text)
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_project_filter_and_unassigned_and_unknown(self):
        self.card(title='Demo only')
        self.card(title='Other only', project='other')
        self.card(title='No project', project='')
        result = self.cli('--project', 'demo')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'Demo only', result.stdout)
        self.assertNotIn(b'Other only', result.stdout)
        self.assertNotIn(b'No project', result.stdout)
        self.assertIn(b'No project', self.cli('--project', '').stdout)
        self.assertIn(b'No cards.', self.cli('--project', 'missing').stdout)
        invalid = self.cli('--project', "demo' OR 1=1")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn(b'Invalid project slug', invalid.stderr)

    def test_order_is_workflow_then_position_then_id(self):
        done = self.card(column='man done', position=-1)
        later = self.card(position=10)
        first = self.card(position=1)
        tied = self.card(position=1)
        text = self.cli().stdout.decode()
        markers = [text.index(f'## Card #{identifier}:') for identifier in [first, tied, later, done]]
        self.assertEqual(markers, sorted(markers))

    def test_markdown_control_characters_and_embedded_fences_remain_literal(self):
        description = '```python\nprint("hello")\n```\n````\n# injected heading\n<script>x</script>'
        self.card(title='[link](url) <script>\n# heading *bold*', description=description)
        text = self.cli().stdout.decode()
        self.assertIn(r'\[link\]\(url\) &lt;script&gt; \# heading \*bold\*', text)
        self.assertIn('`````text\n' + description + '\n`````', text)
        self.assertEqual(text.count('\n## Card #'), 1)

    def test_empty_board(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'# LLM BAN board\n\nNo cards.\n')

    def test_atomic_replacement_and_no_temporary_files(self):
        self.card()
        target = self.root / 'board.md'
        target.write_text('old snapshot')
        result = self.cli('--output', str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(target.read_bytes(), self.cli().stdout)
        self.assertEqual(list(self.root.glob('.llm-ban-export-*')), [])

    def test_failed_replace_preserves_existing_file_and_cleans_temporary_file(self):
        self.card()
        target = self.root / 'board.md'
        target.write_bytes(b'previous snapshot')
        with patch.dict(os.environ, {'LLMBAN_DB': str(self.db)}), \
                patch('board_export.os.replace', side_effect=OSError('simulated failure')):
            with self.assertRaisesRegex(OSError, 'simulated failure'):
                export_board(COLUMNS, output=str(target))
        self.assertEqual(target.read_bytes(), b'previous snapshot')
        self.assertEqual(list(self.root.glob('.llm-ban-export-*')), [])

    def test_failed_write_preserves_existing_file(self):
        target = self.root / 'board.md'
        target.write_bytes(b'previous snapshot')
        with patch.dict(os.environ, {'LLMBAN_DB': str(self.db)}), \
                patch('board_export.os.fsync', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                export_board(COLUMNS, output=str(target))
        self.assertEqual(target.read_bytes(), b'previous snapshot')
        self.assertEqual(list(self.root.glob('.llm-ban-export-*')), [])

    def test_missing_parent_and_database_errors_have_nonzero_exit(self):
        result = self.cli('--output', str(self.root / 'missing' / 'board.md'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'Export failed:', result.stderr)
        missing = self.root / 'missing.sqlite'
        result = self.cli(db=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())
        empty = self.root / 'empty.sqlite'
        with closing(sqlite3.connect(empty)):
            pass
        self.assertNotEqual(self.cli(db=empty).returncode, 0)
        with closing(sqlite3.connect(empty)) as connection:
            self.assertEqual(connection.execute('SELECT name FROM sqlite_master').fetchall(), [])

    def test_cannot_overwrite_database_or_journals(self):
        self.card()
        for suffix in ['', '-wal', '-shm']:
            with self.subTest(suffix=suffix):
                result = self.cli('--output', str(self.db) + suffix)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'must not overwrite', result.stderr)
        self.assertEqual(self.connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main()
