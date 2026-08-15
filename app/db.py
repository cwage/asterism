import os
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "asterism.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    image_path TEXT NOT NULL,
    exif_json TEXT,
    result_json TEXT,
    error TEXT,
    anchors_json TEXT,
    solve_seconds REAL,
    mode TEXT NOT NULL DEFAULT 'quick',
    orphan_recoveries INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0
);

-- Small key/value scratch for things that must survive the machine
-- stopping (auto_stop_machines): currently the notification watermarks
-- (#69), which are needed exactly when the process didn't stay up.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with get_conn() as conn:
        # executescript, not execute: SCHEMA is more than one statement now.
        conn.executescript(SCHEMA)
        # Older databases: bolt missing columns on (SQLite has no
        # ADD COLUMN IF NOT EXISTS). web and worker init concurrently,
        # so losing the ALTER race is fine.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        for name, decl in (
            ("mode", "TEXT NOT NULL DEFAULT 'quick'"),
            ("orphan_recoveries", "INTEGER NOT NULL DEFAULT 0"),
            ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            ("featured", "INTEGER NOT NULL DEFAULT 0"),
            ("anchors_json", "TEXT"),
        ):
            if name not in cols:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
