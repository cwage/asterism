"""Uploader record and daily history (#116): a per-day hash of the client
address instead of the address, camera facts off the file, and counts that
survive the retention sweep. Nothing here may reach a public payload."""

import json

import pytest
from PIL import Image

from app import db, exif, main, notify, stats, worker
from tests.test_moderation import _Req, _auth, _insert, admin, fresh_db  # noqa: F401


def _row(conn, job_id, status="done", created_at="2020-01-01 00:00:00",
         uploader="u1", hidden=0, featured=0, result=None, image_path=None):
    conn.execute(
        "INSERT INTO jobs (id, image_path, status, created_at, uploader_hash, "
        "hidden, featured, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, image_path or f"/uploads/{job_id}.jpg", status, created_at,
         uploader, hidden, featured, json.dumps(result) if result else None))


# --- the hash ---------------------------------------------------------


def test_same_address_same_day_shares_a_hash(fresh_db):
    with db.get_conn() as conn:
        a = stats.uploader_hash(conn, "203.0.113.7", "2026-09-14 10:00:00")
        b = stats.uploader_hash(conn, "203.0.113.7", "2026-09-14 23:59:59")
        c = stats.uploader_hash(conn, "203.0.113.8", "2026-09-14 10:00:00")
    assert a == b
    assert a != c
    assert len(a) == stats.HASH_CHARS
    assert "203" not in a


def test_the_hash_rotates_with_the_day(fresh_db):
    """No joining across days: same address, next day, different token."""
    with db.get_conn() as conn:
        today = stats.uploader_hash(conn, "203.0.113.7", "2026-09-14 12:00:00")
        tomorrow = stats.uploader_hash(conn, "203.0.113.7", "2026-09-15 00:00:01")
    assert today != tomorrow


def test_the_salt_is_minted_once_and_never_logged_as_the_hash(fresh_db):
    with db.get_conn() as conn:
        stats.uploader_hash(conn, "203.0.113.7", "2026-09-14 12:00:00")
        stats.uploader_hash(conn, "203.0.113.9", "2026-09-14 13:00:00")
        salts = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'salt:%'").fetchall()
    assert [s["key"] for s in salts] == ["salt:2026-09-14"]
    assert len(salts[0]["value"]) == 64  # 32 random bytes, hex


def test_no_address_means_no_hash(fresh_db):
    with db.get_conn() as conn:
        assert stats.uploader_hash(conn, None) is None
        assert stats.uploader_hash(conn, "") is None


def test_retiring_salts_keeps_only_today(fresh_db):
    with db.get_conn() as conn:
        stats.uploader_hash(conn, "a", "2026-09-12 12:00:00")
        stats.uploader_hash(conn, "a", "2026-09-13 12:00:00")
        stats.uploader_hash(conn, "a", "2026-09-14 12:00:00")
        assert stats.retire_salts(conn, "2026-09-14 06:00:00") == 2
        left = [r["key"] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'salt:%'")]
    assert left == ["salt:2026-09-14"]


# --- the device record ------------------------------------------------


def test_read_device_takes_make_model_and_software(tmp_path):
    path = tmp_path / "phone.jpg"
    ex = Image.Exif()
    ex[exif.TAG_MAKE] = "Apple"
    ex[exif.TAG_MODEL] = "iPhone 15 Plus\x00"
    ex[exif.TAG_SOFTWARE] = "  26.6  "
    Image.new("RGB", (64, 64)).save(path, exif=ex)
    assert exif.read_device(str(path)) == {
        "make": "Apple", "model": "iPhone 15 Plus", "software": "26.6"}


def test_read_device_is_all_none_without_exif(tmp_path):
    path = tmp_path / "bare.jpg"
    Image.new("RGB", (64, 64)).save(path)
    assert exif.read_device(str(path)) == {
        "make": None, "model": None, "software": None}


def test_read_device_never_touches_the_public_exif_payload(tmp_path):
    """read_exif is what exif_json (and so /jobs/{id}) is built from."""
    path = tmp_path / "phone.jpg"
    ex = Image.Exif()
    ex[exif.TAG_MAKE] = "Apple"
    ex[exif.TAG_MODEL] = "iPhone 15 Plus"
    Image.new("RGB", (64, 64)).save(path, exif=ex)
    info = exif.read_exif(str(path))
    assert not {"make", "model", "software"} & set(info)


# --- the upload path --------------------------------------------------


def test_an_upload_records_the_hash_and_device_but_serves_neither(
        tmp_path, monkeypatch):
    import io
    from fastapi.testclient import TestClient

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path / "uploads"))
    (tmp_path / "uploads").mkdir()
    db.init_db()

    ex = Image.Exif()
    ex[exif.TAG_MAKE] = "Apple"
    ex[exif.TAG_MODEL] = "iPhone 15 Plus"
    ex[exif.TAG_SOFTWARE] = "26.6"
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, "JPEG", exif=ex)

    client = TestClient(main.app)
    headers = {"fly-client-ip": "203.0.113.7"}
    first = client.post("/jobs", files={"image": ("sky.jpg", buf.getvalue(),
                                                  "image/jpeg")}, headers=headers)
    second = client.post("/jobs", files={"image": ("sky.jpg", buf.getvalue(),
                                                   "image/jpeg")}, headers=headers)
    other = client.post("/jobs", files={"image": ("sky.jpg", buf.getvalue(),
                                                  "image/jpeg")},
                        headers={"fly-client-ip": "203.0.113.8"})
    assert first.status_code == second.status_code == other.status_code == 200

    with db.get_conn() as conn:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, uploader_hash, device_json FROM jobs")}
    a, b, c = (rows[first.json()["id"]], rows[second.json()["id"]],
               rows[other.json()["id"]])
    assert a["uploader_hash"] == b["uploader_hash"]
    assert a["uploader_hash"] != c["uploader_hash"]
    assert json.loads(a["device_json"]) == {
        "make": "Apple", "model": "iPhone 15 Plus", "software": "26.6"}

    # The public job payload and the feed know nothing of either.
    public = client.get(f"/jobs/{first.json()['id']}").json()
    assert "uploader_hash" not in json.dumps(public)
    assert "iPhone" not in json.dumps(public)
    assert "iPhone" not in json.dumps(main.feed())


# --- the daily history ------------------------------------------------


def test_the_sweep_rolls_rows_into_daily_stats_before_deleting(fresh_db, tmp_path):
    with db.get_conn() as conn:
        _row(conn, "a", created_at="2026-08-13 21:00:00", uploader="u1")
        _row(conn, "b", created_at="2026-08-13 22:00:00", uploader="u1")
        _row(conn, "c", status="failed", created_at="2026-08-13 23:00:00",
             uploader="u2", result={"failure": {"reason": "no_stars"}})
        _row(conn, "d", created_at="2026-08-13 23:30:00", uploader="u3", hidden=1)
        _row(conn, "e", created_at="2026-08-14 01:00:00", uploader="u1")
        _row(conn, "f", status="failed", created_at="2026-08-14 02:00:00",
             uploader=None, result={"failure": {"reason": "no_match"}})

    assert worker.sweep_expired() == 6

    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        history = stats.history(conn)
    assert history == [
        {"day": "2026-08-14", "uploads": 2, "solved": 1, "failed": 1,
         "hidden": 0, "reasons": {"no_match": 1}, "uploaders": 1},
        {"day": "2026-08-13", "uploads": 4, "solved": 3, "failed": 1,
         "hidden": 1, "reasons": {"no_stars": 1}, "uploaders": 3},
    ]


def test_a_featured_row_is_counted_once_and_kept(fresh_db, admin, tmp_path):
    img = tmp_path / "keep.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _row(conn, "keep", created_at="2026-08-13 21:00:00", uploader="u1",
             image_path=str(img))
    main.feature_job("keep", _auth())

    for _ in range(3):
        assert worker.sweep_expired() == 0
    assert img.exists()
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT counted FROM jobs WHERE id = 'keep'").fetchone()[0] == 1
        assert stats.history(conn) == [
            {"day": "2026-08-13", "uploads": 1, "solved": 1, "failed": 0,
             "hidden": 0, "reasons": {}, "uploaders": 1}]


def test_rows_still_inside_the_window_are_not_counted_yet(fresh_db):
    with db.get_conn() as conn:
        now = conn.execute("SELECT datetime('now')").fetchone()[0]
        _row(conn, "fresh", created_at=now, uploader="u1")
    assert worker.sweep_expired() == 0
    with db.get_conn() as conn:
        assert stats.history(conn) == []
        assert conn.execute(
            "SELECT counted FROM jobs WHERE id = 'fresh'").fetchone()[0] == 0


def test_the_sweep_retires_past_salts(fresh_db):
    with db.get_conn() as conn:
        stats.uploader_hash(conn, "a", "2020-01-01 12:00:00")
        today = stats.uploader_hash(conn, "a")
    worker.sweep_expired()
    with db.get_conn() as conn:
        keys = [r["key"] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'salt:%'")]
        assert len(keys) == 1 and keys[0] != "salt:2020-01-01"
        # ...and today's still hashes the same way.
        assert stats.uploader_hash(conn, "a") == today


def test_history_limit_returns_the_newest_days(fresh_db):
    with db.get_conn() as conn:
        for day in ("2026-08-11", "2026-08-12", "2026-08-13"):
            _row(conn, day, created_at=f"{day} 12:00:00")
    worker.sweep_expired()
    with db.get_conn() as conn:
        assert [h["day"] for h in stats.history(conn, days=2)] == [
            "2026-08-13", "2026-08-12"]


# --- the digest -------------------------------------------------------


def test_summary_counts_distinct_people(fresh_db):
    with db.get_conn() as conn:
        recent = conn.execute(
            "SELECT datetime('now', '-1 hours')").fetchone()[0]
        _row(conn, "a", created_at=recent, uploader="u1")
        _row(conn, "b", created_at=recent, uploader="u1")
        _row(conn, "c", created_at=recent, uploader="u2")
        _row(conn, "d", created_at=recent, uploader=None)
        since = conn.execute(
            "SELECT datetime('now', '-24 hours')").fetchone()[0]
        counts = notify.activity_counts(conn, since)
    assert counts["uploads"] == 4
    assert counts["uploaders"] == 2
    assert notify.format_summary(counts).startswith("4 uploads from 2 people")


def test_summary_says_person_for_one_and_nothing_for_none():
    base = {"solved": 0, "failed": 0, "hidden": 0, "featured": 0, "reasons": {}}
    assert notify.format_summary({"uploads": 3, "uploaders": 1, **base}
                                 ).startswith("3 uploads from 1 person")
    assert notify.format_summary({"uploads": 0, "uploaders": 0, **base}
                                 ).startswith("0 uploads ·")
