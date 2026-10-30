"""Shared, idempotent SQLite initialization."""
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def db_path():
    return Path(os.environ.get('LLMBAN_DB', ROOT / 'data' / 'llmban.sqlite'))


def connect(path=None):
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


def initialize(c):
    c.executescript((ROOT / 'migrations' / '001_board.sql').read_text())
    from memctl import ensure_schema
    ensure_schema(c)


def event(c, card_id, kind, payload):
    c.execute('INSERT INTO events(card_id,event_type,payload,created_at) VALUES(?,?,?,?)',
              (card_id, kind, payload, now()))
