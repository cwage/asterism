"""Uploader keep (#113): whoever holds a result link can exempt a solved
photo from the retention sweep while the window is open, and withdraw it.
No admin token; a per-address daily ceiling instead."""

import pytest
from fastapi import HTTPException

from app import db, main, worker
from tests.test_moderation import _auth, _insert, admin, fresh_db  # noqa: F401


class _Req:
    """Just enough Request for _client_ip."""

    def __init__(self, ip="203.0.113.7"):
        self.headers = {"fly-client-ip": ip}
        self.client = None


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(main, "_keep_log", main._keep_log.__class__(main._keep_log.default_factory))


def _fresh(conn, job_id, **kw):
    """A job inside the retention window."""
    conn.execute("UPDATE jobs SET created_at = datetime('now') WHERE id = ?", (job_id,))


def _age_out(conn, job_id):
    conn.execute("UPDATE jobs SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
                 (job_id,))


def test_a_kept_solve_survives_the_sweep(fresh_db, tmp_path):
    img = tmp_path / "keep.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _insert(conn, "mine", image_path=str(img), result={"labels": []})
        _fresh(conn, "mine")

    assert main.keep_job("mine", _Req()) == {"id": "mine", "kept": True}
    job = main.get_job("mine")
    assert job["kept"] is True and job["keep_open"] is True

    with db.get_conn() as conn:
        _age_out(conn, "mine")
    for _ in range(2):
        assert worker.sweep_expired() == 0
    assert img.exists()
    # and the page still finds it, past the window
    assert main.get_job("mine")["kept"] is True
    assert main.get_job("mine")["keep_open"] is False


def test_unkeep_lets_the_sweep_collect_it(fresh_db, tmp_path):
    img = tmp_path / "keep.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _insert(conn, "mine", image_path=str(img))
        _fresh(conn, "mine")
    main.keep_job("mine", _Req())
    with db.get_conn() as conn:
        _age_out(conn, "mine")
    assert worker.sweep_expired() == 0

    assert main.unkeep_job("mine") == {"id": "mine", "kept": False}

    assert worker.sweep_expired() == 1
    assert not img.exists()


def test_keep_needs_a_solved_job_inside_the_window(fresh_db):
    with db.get_conn() as conn:
        _insert(conn, "failed", status="failed")
        _insert(conn, "queued", status="queued")
        _insert(conn, "late")  # the default created_at is long past
        _fresh(conn, "failed")
        _fresh(conn, "queued")
    for job_id in ("failed", "queued", "late"):
        with pytest.raises(HTTPException) as e:
            main.keep_job(job_id, _Req())
        assert e.value.status_code == 409
    assert "closed" in e.value.detail  # the last one: the window, not the status
    assert main.get_job("late")["keep_open"] is False
    with pytest.raises(HTTPException) as e:
        main.keep_job("nope", _Req())
    assert e.value.status_code == 404


def test_hidden_jobs_cannot_be_kept_and_hiding_clears_a_keep(fresh_db, admin, tmp_path):
    img = tmp_path / "bad.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _insert(conn, "bad", image_path=str(img))
        _fresh(conn, "bad")
    main.keep_job("bad", _Req())

    main.hide_job("bad", _auth())

    with db.get_conn() as conn:
        row = conn.execute("SELECT hidden, kept FROM jobs WHERE id='bad'").fetchone()
    assert (row["hidden"], row["kept"]) == (1, 0)
    # a hidden job is indistinguishable from a missing one, to keep too
    for call in (lambda: main.keep_job("bad", _Req()), lambda: main.unkeep_job("bad")):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404
    with db.get_conn() as conn:
        _age_out(conn, "bad")
    assert worker.sweep_expired() == 1


def test_unkeep_leaves_an_operator_feature_alone(fresh_db, admin):
    with db.get_conn() as conn:
        _insert(conn, "show")
        _fresh(conn, "show")
    main.feature_job("show", _auth())
    main.keep_job("show", _Req())
    main.unkeep_job("show")
    with db.get_conn() as conn:
        row = conn.execute("SELECT featured, kept FROM jobs WHERE id='show'").fetchone()
    assert (row["featured"], row["kept"]) == (1, 0)


def test_keeps_are_rate_limited_per_address(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "KEEPS_PER_DAY", 2)
    with db.get_conn() as conn:
        for i in range(3):
            _insert(conn, f"j{i}")
            _fresh(conn, f"j{i}")
    main.keep_job("j0", _Req())
    main.keep_job("j1", _Req())
    with pytest.raises(HTTPException) as e:
        main.keep_job("j2", _Req())
    assert e.value.status_code == 429
    # another address is unaffected, and a rejected attempt still counted
    assert main.keep_job("j2", _Req(ip="198.51.100.9")) == {"id": "j2", "kept": True}
    assert main._keep_limited("203.0.113.7") is True


def test_job_json_says_when_keeping_is_on_offer(fresh_db):
    with db.get_conn() as conn:
        _insert(conn, "fresh")
        _fresh(conn, "fresh")
        _insert(conn, "failing", status="failed")
        _fresh(conn, "failing")
    assert main.get_job("fresh")["keep_open"] is True
    assert main.get_job("fresh")["kept"] is False
    assert main.get_job("failing")["keep_open"] is False
