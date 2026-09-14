"""The same file twice is solved once (#120): an upload is hashed as it
arrives, and a second send of the same bytes gets the first job back."""

import hashlib
import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db, exif, main
from tests.test_moderation import fresh_db  # noqa: F401


@pytest.fixture()
def client(fresh_db, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path / "uploads"))
    (tmp_path / "uploads").mkdir()
    # Every test here uploads several times from the one test address.
    monkeypatch.setattr(main, "_upload_log", type(main._upload_log)(
        main._upload_log.default_factory))
    return TestClient(main.app)


def _jpeg(shade=0, orientation=None):
    ex = Image.Exif()
    if orientation:
        ex[exif.TAG_ORIENTATION] = orientation
    buf = io.BytesIO()
    Image.new("RGB", (64, 32), (shade, shade, shade)).save(
        buf, "JPEG", exif=ex, quality=95)
    return buf.getvalue()


def _post(client, data):
    resp = client.post("/jobs", files={"image": ("sky.jpg", data, "image/jpeg")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(conn):
    return conn.execute(
        "SELECT id, status, content_hash, image_path FROM jobs ORDER BY rowid"
    ).fetchall()


def test_the_same_bytes_get_the_same_job(client):
    data = _jpeg()
    first = _post(client, data)
    second = _post(client, data)

    assert second["id"] == first["id"]
    assert second["duplicate"] is True
    assert second["status"] == "queued"
    assert "duplicate" not in first
    with db.get_conn() as conn:
        rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["content_hash"] == hashlib.sha256(data).hexdigest()
    # Nothing was written for the second send: no file, no bake.
    assert os.listdir(main.UPLOAD_DIR) == [os.path.basename(rows[0]["image_path"])]


def test_hashed_as_received_not_as_stored(client):
    """The bake re-encodes a rotated upload, so the stored bytes are not the
    sent bytes. The hash is over what the phone sent, which is what it
    will send again."""
    data = _jpeg(orientation=6)
    first = _post(client, data)
    with db.get_conn() as conn:
        row = _rows(conn)[0]
    with open(row["image_path"], "rb") as f:
        stored = f.read()
    assert stored != data
    assert row["content_hash"] == hashlib.sha256(data).hexdigest()

    assert _post(client, data)["id"] == first["id"]
    assert _post(client, stored)["id"] != first["id"]


def test_different_bytes_are_different_jobs(client):
    a = _post(client, _jpeg(shade=10))
    b = _post(client, _jpeg(shade=20))
    assert a["id"] != b["id"]
    with db.get_conn() as conn:
        rows = _rows(conn)
    assert len(rows) == 2
    assert rows[0]["content_hash"] != rows[1]["content_hash"]


def test_a_hidden_job_never_comes_back(client):
    """A takedown (#60) must not be undone by sending the file again: the
    re-upload gets a fresh job, which hides the same way."""
    data = _jpeg()
    hidden = _post(client, data)["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET hidden = 1 WHERE id = ?", (hidden,))

    again = _post(client, data)
    assert again["id"] != hidden
    assert "duplicate" not in again
    with db.get_conn() as conn:
        rows = _rows(conn)
    assert [r["id"] for r in rows] == [hidden, again["id"]]
    assert rows[0]["content_hash"] == rows[1]["content_hash"]


def test_a_match_is_the_answer_whatever_state_it_is_in(client):
    """A failed job fails the same way for the same bytes, and says so at
    once (the deepen button is still there); a featured one outlives the
    retention window and still answers."""
    data = _jpeg()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, image_path, status, created_at, featured, "
            "content_hash) VALUES ('old', '/uploads/old.jpg', 'failed', "
            "'2020-01-01 00:00:00', 1, ?)",
            (hashlib.sha256(data).hexdigest(),))

    resp = _post(client, data)
    assert resp == {"id": "old", "status": "failed", "duplicate": True}
    assert os.listdir(main.UPLOAD_DIR) == []


def test_two_sends_seconds_apart_share_one_row(client, monkeypatch):
    """The motivating case: one frame sent twice within seconds. The second
    send passes the early check (the first has no row until its bake is
    done), so the check is made again under the write lock at insert
    time, and the second yields to the first's row."""
    data = _jpeg()
    digest = hashlib.sha256(data).hexdigest()
    real_bake = exif.normalize_orientation
    landed = []

    def rival_lands_during_our_bake(path):
        changed = real_bake(path)
        if not landed:
            landed.append(True)
            with db.get_conn() as conn:
                conn.execute(
                    "INSERT INTO jobs (id, image_path, content_hash) "
                    "VALUES ('rival', '/uploads/rival.jpg', ?)", (digest,))
        return changed

    monkeypatch.setattr(exif, "normalize_orientation", rival_lands_during_our_bake)
    resp = _post(client, data)

    assert resp == {"id": "rival", "status": "queued", "duplicate": True}
    with db.get_conn() as conn:
        rows = _rows(conn)
    assert [r["id"] for r in rows] == ["rival"]
    # Our upload was written and baked, then discarded: only its bake was spent.
    assert os.listdir(main.UPLOAD_DIR) == []


def test_a_full_queue_still_answers_a_duplicate(client, monkeypatch):
    """A re-upload enqueues nothing, so the capacity gate is not its
    business; a new upload still meets it."""
    data = _jpeg()
    first = _post(client, data)
    monkeypatch.setattr(main, "MAX_QUEUE_DEPTH", 0)

    again = _post(client, data)
    assert again["id"] == first["id"] and again["duplicate"] is True
    fresh = client.post("/jobs", files={"image": ("sky.jpg", _jpeg(shade=9),
                                                  "image/jpeg")})
    assert fresh.status_code == 503, fresh.text
    with db.get_conn() as conn:
        assert len(_rows(conn)) == 1


def test_the_hash_is_not_served(client):
    data = _jpeg()
    job_id = _post(client, data)["id"]
    digest = hashlib.sha256(data).hexdigest()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done', result_json = ? WHERE id = ?",
            (json.dumps({"labels": []}), job_id))

    public = json.dumps(client.get(f"/jobs/{job_id}").json())
    assert "content_hash" not in public
    assert digest not in public
    assert digest not in json.dumps(main.feed())
