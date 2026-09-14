"""Schema migration: older databases gain missing columns on init."""

import sqlite3

from app import db


def test_init_db_adds_mode_column_to_old_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))

    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL "
        "DEFAULT 'queued', created_at TEXT, image_path TEXT NOT NULL, "
        "exif_json TEXT, result_json TEXT, error TEXT, solve_seconds REAL)"
    )
    conn.execute("INSERT INTO jobs (id, image_path) VALUES ('old1', '/x.jpg')")
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # idempotent

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT mode, orphan_recoveries, uploader_hash, device_json, "
            "counted FROM jobs WHERE id = 'old1'"
        ).fetchone()
        assert row["mode"] == "quick"
        assert row["orphan_recoveries"] == 0
        # Uploader record (#116): nullable facts, and an existing row has
        # not been folded into the daily history yet.
        assert row["uploader_hash"] is None
        assert row["device_json"] is None
        assert row["counted"] == 0
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"meta", "daily_stats", "daily_uploaders"} <= tables
