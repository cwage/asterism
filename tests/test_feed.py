"""Public "recently solved" feed: done jobs only, newest first, capped,
with the narration caption riding along when the worker produced one."""

import json

import pytest

from app import db, main


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()


def _insert(conn, job_id, status, created_at, result=None):
    conn.execute(
        "INSERT INTO jobs (id, image_path, status, created_at, result_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, f"/uploads/{job_id}.jpg", status, created_at,
         json.dumps(result) if result else None),
    )


def test_feed_lists_done_jobs_newest_first(fresh_db):
    with db.get_conn() as conn:
        _insert(conn, "older", "done", "2026-08-13 21:00:00",
                {"labels": []})
        _insert(conn, "newer", "done", "2026-08-13 22:00:00",
                {"labels": [], "narration": {"caption": "Jupiter rising",
                                             "text": "…", "model": "m"}})
        _insert(conn, "nope1", "failed", "2026-08-13 23:00:00")
        _insert(conn, "nope2", "queued", "2026-08-13 23:00:00")
        _insert(conn, "nope3", "solving", "2026-08-13 23:00:00")

    jobs = main.feed()["jobs"]
    assert [j["id"] for j in jobs] == ["newer", "older"]
    # caption only when narration exists; never a null placeholder
    assert jobs[0]["caption"] == "Jupiter rising"
    assert "caption" not in jobs[1]
    # nothing beyond id/created_at/caption leaks (no exif, no result)
    assert set(jobs[0]) == {"id", "created_at", "caption"}


def test_feed_is_capped(fresh_db):
    with db.get_conn() as conn:
        for i in range(main.FEED_LIMIT + 5):
            _insert(conn, f"job{i:03}", "done", f"2026-08-13 10:{i:02}:00")
    jobs = main.feed()["jobs"]
    assert len(jobs) == main.FEED_LIMIT
    # the newest survive the cap
    assert jobs[0]["id"] == f"job{main.FEED_LIMIT + 4:03}"


def test_feed_empty_db(fresh_db):
    assert main.feed() == {"jobs": []}
