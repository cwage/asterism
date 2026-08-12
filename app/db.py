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
    solve_seconds REAL,
    mode TEXT NOT NULL DEFAULT 'quick'
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
        conn.execute(SCHEMA)
        # Pre-`mode` databases: bolt the column on (SQLite has no
        # ADD COLUMN IF NOT EXISTS). web and worker init concurrently,
        # so losing the ALTER race is fine.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        if "mode" not in cols:
            try:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'quick'"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
