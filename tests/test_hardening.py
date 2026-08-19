"""Public-endpoint hardening: per-IP rate limiting (#10) and the retention
sweep (#23)."""

import json
import os

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


def test_quick_jobs_jump_deep_jobs_in_queue(tmp_path, monkeypatch):
    """Claim order and queue positions agree: quick before deep, FIFO within
    each class, however the arrival times interleave."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO jobs (id, image_path, status, mode, created_at) VALUES "
                     "('deep-early', '/a.jpg', 'queued', 'deep', '2026-08-19 05:00:00')")
        conn.execute("INSERT INTO jobs (id, image_path, status, mode, created_at) VALUES "
                     "('quick-late', '/b.jpg', 'queued', 'quick', '2026-08-19 05:01:00')")
        conn.execute("INSERT INTO jobs (id, image_path, status, mode, created_at) VALUES "
                     "('quick-later', '/c.jpg', 'queued', 'quick', '2026-08-19 05:02:00')")

    # The deep job arrived first but reports both quick jobs ahead of it.
    assert main.get_job("quick-late")["queue_position"] == 0
    assert main.get_job("quick-later")["queue_position"] == 1
    assert main.get_job("deep-early")["queue_position"] == 2

    with db.get_conn() as conn:
        assert worker.claim_next_job(conn)["id"] == "quick-late"
        assert worker.claim_next_job(conn)["id"] == "quick-later"
        assert worker.claim_next_job(conn)["id"] == "deep-early"
        assert worker.claim_next_job(conn) is None


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


def test_upload_accepts_a_file_piexif_cannot_re_encode(tmp_path, monkeypatch):
    """The whole point of the strip is that the stored file must not carry
    coordinates. Rejecting an upload because *one library* could not parse
    it fails a user over an implementation detail — a real report, on a
    JPEG whose float-valued ExposureTime makes piexif raise."""
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from app import exif as exif_mod, main
    from tests import synth

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path / "uploads"))
    os.makedirs(main.UPLOAD_DIR, exist_ok=True)
    db.init_db()

    ex = Image.Exif()
    ifd = ex.get_ifd(synth.EXIF_IFD)
    ifd[synth.TAG_EXPOSURE_TIME] = 10.0          # the trap
    ifd[synth.TAG_FOCAL_35MM] = 39
    gps = ex.get_ifd(synth.GPS_IFD)
    gps[1], gps[2] = "N", synth._deg_to_dms(36.16)
    gps[3], gps[4] = "W", synth._deg_to_dms(86.78)
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, format="JPEG", exif=ex, quality=92)

    client = TestClient(main.app)
    resp = client.post("/jobs", files={"image": ("sky.jpg", buf.getvalue(),
                                                 "image/jpeg")})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["id"]

    # The coordinates reached the job record...
    with db.get_conn() as conn:
        row = conn.execute("SELECT image_path, exif_json FROM jobs WHERE id = ?",
                           (job_id,)).fetchone()
    assert json.loads(row["exif_json"])["lat"] == pytest.approx(36.16, abs=0.01)
    # ...and left the file that gets served.
    assert exif_mod.has_location(row["image_path"]) is False


def test_upload_is_refused_when_the_location_really_cannot_be_removed(
        tmp_path, monkeypatch):
    """Fail closed still means closed: if every strip leaves coordinates in
    the file, the upload is rejected rather than served."""
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from app import exif as exif_mod, main
    from tests import synth

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path / "uploads"))
    os.makedirs(main.UPLOAD_DIR, exist_ok=True)
    db.init_db()
    monkeypatch.setattr(exif_mod, "strip_gps",
                        lambda path: (_ for _ in ()).throw(RuntimeError("nope")))

    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(
        buf, format="JPEG", exif=synth.build_exif(f35mm=39, gps=(36.16, -86.78)))

    client = TestClient(main.app)
    resp = client.post("/jobs", files={"image": ("sky.jpg", buf.getvalue(),
                                                 "image/jpeg")})
    assert resp.status_code == 415
    assert "strip location data" in resp.json()["detail"]
    assert os.listdir(main.UPLOAD_DIR) == []      # nothing left on disk
