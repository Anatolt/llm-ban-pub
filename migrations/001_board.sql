PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    column_name TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO settings VALUES ('language', 'English');
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    card_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    state TEXT NOT NULL,
    original_title TEXT NOT NULL,
    original_description TEXT NOT NULL,
    working_column TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    report TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run
    ON agent_runs((1)) WHERE state='running';
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    sent_at TEXT
);
