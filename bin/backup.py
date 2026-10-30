#!/usr/bin/env python3
"""Consistent SQLite backup; no overwrite, pruning or automatic deletion."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid
from storage import connect, db_path

directory = db_path().parent / 'backups'
directory.mkdir(parents=True, exist_ok=True)
target = directory / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.sqlite')
target.touch(exist_ok=False, mode=0o600)
source = connect()
destination = sqlite3.connect(target)
try:
    source.backup(destination)
    assert destination.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
finally:
    source.close()
    destination.close()
print(target)
