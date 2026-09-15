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
    mode TEXT NOT NULL DEFAULT 'quick',
    orphan_recoveries INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0,
    -- Kept by its uploader (#113): exempt from the sweep like featured, but
    -- set from the result page by whoever holds the link, and never by the
    -- operator, so the two can be told apart and undone separately.
    kept INTEGER NOT NULL DEFAULT 0,
    -- Uploader record (#116): a per-day HMAC of the client address, the
    -- camera facts the served file carries anyway, and whether the row has
    -- been folded into daily_stats yet.
    uploader_hash TEXT,
    device_json TEXT,
    counted INTEGER NOT NULL DEFAULT 0,
    -- SHA-256 of the upload as received (#120): a second send of the same
    -- bytes is answered with this row instead of a second solve.
    content_hash TEXT
);

-- Daily history (#116), written by the retention sweep before it deletes
-- the rows it was computed from. Keyed by the UTC day of created_at.
CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT PRIMARY KEY,
    uploads INTEGER NOT NULL DEFAULT 0,
    solved INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    reasons_json TEXT NOT NULL DEFAULT '{}'
);

-- The day's distinct uploader hashes. Opaque once the day's salt is gone.
CREATE TABLE IF NOT EXISTS daily_uploaders (
    day TEXT NOT NULL,
    uploader_hash TEXT NOT NULL,
    PRIMARY KEY (day, uploader_hash)
);

-- The numbers behind every finished solve (#99), one row per job and no
-- image data, outside the retention sweep: thresholds come from a
-- distribution instead of a memory. Written when a job finishes; a
-- deepen overwrites its row.
CREATE TABLE IF NOT EXISTS solve_stats (
    job_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    mode TEXT,
    reason TEXT,
    logodds REAL,
    nmatch INTEGER,
    ndistract INTEGER,
    stars_detected INTEGER,
    attempts INTEGER,
    thorough_attempts INTEGER,
    timed_out INTEGER,
    tier_lo REAL,
    tier_hi REAL,
    seconds REAL,
    exif_fov_deg REAL,
    fitted_fov_deg REAL,
    stars_matched INTEGER,
    stars_hidden INTEGER,
    warped INTEGER,
    limiting_mag REAL,
    time_source TEXT,
    has_tilt INTEGER,
    make TEXT
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
            ("kept", "INTEGER NOT NULL DEFAULT 0"),
            ("uploader_hash", "TEXT"),
            ("device_json", "TEXT"),
            ("counted", "INTEGER NOT NULL DEFAULT 0"),
            ("content_hash", "TEXT"),
        ):
            if name not in cols:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
        # After the ALTERs, not in SCHEMA: on an older database the column
        # is not there until the loop above adds it.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS jobs_content_hash ON jobs (content_hash)"
        )
