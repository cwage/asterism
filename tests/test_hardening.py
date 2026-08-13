"""Public-endpoint hardening: per-IP rate limiting (#10) and the retention
sweep (#23)."""

import pytest

from app import db, main, worker


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(main, "_upload_log", type(main._upload_log)(
        main._upload_log.default_factory))


def test_rate_limit_allows_burst_then_blocks(monkeypatch):
    monkeypatch.setattr(main, "UPLOADS_PER_HOUR", 3)
    assert not main._rate_limited("1.2.3.4", now=1000.0)
    assert not main._rate_limited("1.2.3.4", now=1001.0)
    assert not main._rate_limited("1.2.3.4", now=1002.0)
    assert main._rate_limited("1.2.3.4", now=1003.0)
    # other clients are unaffected
    assert not main._rate_limited("5.6.7.8", now=1003.0)


def test_rate_limit_window_slides(monkeypatch):
    monkeypatch.setattr(main, "UPLOADS_PER_HOUR", 2)
    assert not main._rate_limited("ip", now=0.0)
    assert not main._rate_limited("ip", now=10.0)
    assert main._rate_limited("ip", now=20.0)
    # first attempt ages out of the 1h window; a slot frees up
    assert not main._rate_limited("ip", now=3601.0)


def test_sweep_expired_removes_old_jobs_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    old_img = uploads / "old.jpg"
    new_img = uploads / "new.jpg"
    old_img.write_bytes(b"x")
    new_img.write_bytes(b"x")
    old_dir = tmp_path / "jobs" / "oldjob"
    old_dir.mkdir(parents=True)
    (old_dir / "solve.wcs").write_bytes(b"x")

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, image_path, created_at) VALUES "
            "('oldjob', ?, datetime('now', '-25 hours'))", (str(old_img),))
        conn.execute(
            "INSERT INTO jobs (id, image_path) VALUES ('newjob', ?)",
            (str(new_img),))

    assert worker.sweep_expired() == 1

    assert not old_img.exists()
    assert not old_dir.exists()
    assert new_img.exists()
    with db.get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM jobs")]
    assert ids == ["newjob"]


def test_queue_position_counts_solving_and_earlier_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO jobs (id, image_path, status, created_at) VALUES "
                     "('busy', '/a.jpg', 'solving', '2026-08-12 05:00:00')")
        conn.execute("INSERT INTO jobs (id, image_path, status, created_at) VALUES "
                     "('first', '/b.jpg', 'queued', '2026-08-12 05:01:00')")
        # same created_at as 'later': id breaks the tie, matching the worker
        conn.execute("INSERT INTO jobs (id, image_path, status, created_at) VALUES "
                     "('later', '/c.jpg', 'queued', '2026-08-12 05:02:00')")
        conn.execute("INSERT INTO jobs (id, image_path, status, created_at) VALUES "
                     "('tied', '/d.jpg', 'queued', '2026-08-12 05:02:00')")

    assert main.get_job("first")["queue_position"] == 1   # just the solver
    assert main.get_job("later")["queue_position"] == 2   # solver + first
    assert main.get_job("tied")["queue_position"] == 3    # 'later' < 'tied' by id
    assert "queue_position" not in main.get_job("busy")   # solving, not queued


def test_orphaned_solving_jobs_requeued_on_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO jobs (id, image_path, status) VALUES "
                     "('stuck', '/x.jpg', 'solving')")
        conn.execute("INSERT INTO jobs (id, image_path, status) VALUES "
                     "('fine', '/y.jpg', 'done')")

    assert worker.recover_orphans() == (1, 0)

    with db.get_conn() as conn:
        rows = {r["id"]: (r["status"], r["orphan_recoveries"])
                for r in conn.execute("SELECT * FROM jobs")}
    assert rows == {"stuck": ("queued", 1), "fine": ("done", 0)}


def test_repeatedly_orphaned_job_is_abandoned(tmp_path, monkeypatch):
    """A job whose solve keeps crashing the worker (e.g. an OOM-inducing
    image) must not be re-queued forever — that turns one bad upload into
    a machine boot loop."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO jobs (id, image_path, status, orphan_recoveries) "
                     "VALUES ('poison', '/x.jpg', 'solving', ?)",
                     (worker.MAX_ORPHAN_RECOVERIES,))
        conn.execute("INSERT INTO jobs (id, image_path, status) VALUES "
                     "('stuck', '/y.jpg', 'solving')")

    assert worker.recover_orphans() == (1, 1)

    with db.get_conn() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["poison"]["status"] == "failed"
    assert "interrupted repeatedly" in rows["poison"]["error"]
    assert rows["stuck"]["status"] == "queued"


def test_sweep_survives_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, image_path, created_at) VALUES "
            "('ghost', '/nonexistent/x.jpg', datetime('now', '-25 hours'))")
    assert worker.sweep_expired() == 1
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
