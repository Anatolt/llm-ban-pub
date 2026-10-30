import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def test_online_backup_captures_wal_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'source.sqlite'
            c = sqlite3.connect(db)
            try:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('CREATE TABLE evidence(value TEXT)')
                c.execute("INSERT INTO evidence VALUES('persisted')")
                c.commit()
                env = dict(os.environ, LLMBAN_DB=str(db))
                paths = []
                for _ in range(2):
                    output = subprocess.check_output([sys.executable, str(ROOT / 'bin' / 'backup.py')], env=env, text=True)
                    paths.append(Path(output.strip()))
                self.assertNotEqual(paths[0], paths[1])
                for path in paths:
                    backup = sqlite3.connect(path)
                    try:
                        self.assertEqual(backup.execute('SELECT value FROM evidence').fetchone()[0], 'persisted')
                        self.assertEqual(backup.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                    finally:
                        backup.close()
            finally:
                c.close()
