"""Moderation kill switch (#60): a token-authed hide that pulls a job out of
every public read path, without waiting on the retention sweep."""

import json

import pytest
from fastapi import HTTPException

from app import db, main, worker


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()


@pytest.fixture()
def admin(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "s3cret")


class _Req:
    """Just enough Request for _require_admin."""

    def __init__(self, authorization=None):
        self.headers = {"authorization": authorization} if authorization else {}


def _auth(token="s3cret"):
    return _Req(f"Bearer {token}")


def _insert(conn, job_id, status="done", image_path="/uploads/x.jpg",
            created_at="2026-08-13 21:00:00", result=None):
    conn.execute(
        "INSERT INTO jobs (id, image_path, status, created_at, result_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, image_path, status, created_at,
         json.dumps(result) if result else None),
    )


def test_hide_removes_job_from_every_read_path(fresh_db, admin, tmp_path):
    upload = tmp_path / "shown.jpg"
    upload.write_bytes(b"x")
    with db.get_conn() as conn:
        _insert(conn, "bad", image_path=str(upload), result={"labels": []})
        _insert(conn, "good", image_path=str(upload), result={"labels": []})

    assert main.hide_job("bad", _auth()) == {"id": "bad", "hidden": True}

    # feed, status, image, and card all behave as if it never existed
    assert [j["id"] for j in main.feed()["jobs"]] == ["good"]
    for call in (lambda: main.get_job("bad"),
                 lambda: main.get_job_image("bad"),
                 lambda: main.get_job_card("bad", _Req())):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404
    # ...and the untouched job still works
    assert main.get_job("good")["status"] == "done"


def test_hide_unlinks_the_cached_card(fresh_db, admin, tmp_path):
    """The card is the amplification path: ?job= unfurls it via OpenGraph,
    so it must not outlive the hide."""
    upload = tmp_path / "shown.jpg"
    upload.write_bytes(b"x")
    card = tmp_path / "shown.jpg.card.png"
    card.write_bytes(b"png")
    with db.get_conn() as conn:
        _insert(conn, "bad", image_path=str(upload))

    main.hide_job("bad", _auth())

    assert not card.exists()
    assert upload.exists()  # the upload itself waits for the sweep


def test_hide_survives_a_job_with_no_rendered_card(fresh_db, admin, tmp_path):
    upload = tmp_path / "shown.jpg"
    upload.write_bytes(b"x")
    with db.get_conn() as conn:
        _insert(conn, "bad", image_path=str(upload))
    assert main.hide_job("bad", _auth())["hidden"] is True


def test_hide_is_reversible(fresh_db, admin):
    """Wrong id typed at 2am: one UPDATE puts it back."""
    with db.get_conn() as conn:
        _insert(conn, "oops", result={"labels": []})
    main.hide_job("oops", _auth())
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET hidden = 0 WHERE id = 'oops'")
    assert [j["id"] for j in main.feed()["jobs"]] == ["oops"]


def test_hide_rejects_bad_and_missing_tokens(fresh_db, admin):
    with db.get_conn() as conn:
        _insert(conn, "job")
    # includes a non-ASCII header: compare_digest on str would raise TypeError
    # and turn a junk request into a 500 on a public endpoint.
    for request in (_Req(), _Req("Bearer wrong"), _Req("s3cret"),
                    _Req("Basic s3cret"), _Req("Bearer café"), _Req("")):
        with pytest.raises(HTTPException) as e:
            main.hide_job("job", request)
        assert e.value.status_code == 404
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT hidden FROM jobs WHERE id = 'job'").fetchone()["hidden"] == 0


def test_hide_endpoint_is_absent_when_no_token_configured(fresh_db, monkeypatch):
    """Unconfigured means gone, not open: an empty ADMIN_TOKEN must not make
    every caller an admin."""
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    with db.get_conn() as conn:
        _insert(conn, "job")
    for request in (_Req(), _auth(), _Req("Bearer ")):
        with pytest.raises(HTTPException) as e:
            main.hide_job("job", request)
        assert e.value.status_code == 404


def test_hide_unknown_job_404s(fresh_db, admin):
    with pytest.raises(HTTPException) as e:
        main.hide_job("nosuchjob", _auth())
    assert e.value.status_code == 404


def test_hidden_job_cannot_be_deepened(fresh_db, admin):
    """Otherwise a hidden job walks itself back into the queue."""
    with db.get_conn() as conn:
        _insert(conn, "bad", status="failed")
    main.hide_job("bad", _auth())
    with pytest.raises(HTTPException) as e:
        main.deepen_job("bad")
    assert e.value.status_code == 404


def test_hidden_job_is_never_claimed_by_the_worker(fresh_db, admin):
    """Hiding a still-queued upload must stop the solve, not just the display
    — a solve is ~45s of the one shared CPU."""
    with db.get_conn() as conn:
        _insert(conn, "bad", status="queued", created_at="2026-08-13 20:00:00")
        _insert(conn, "fine", status="queued", created_at="2026-08-13 21:00:00")
    main.hide_job("bad", _auth())

    with db.get_conn() as conn:
        job = worker.claim_next_job(conn)
    assert job["id"] == "fine"  # 'bad' is older but skipped


class _HideMidClaim:
    """Connection wrapper that commits a hide the instant the claim's SELECT
    runs — reproducing the one interleaving the conditional UPDATE exists to
    defend against."""

    def __init__(self, conn, job_id):
        self._conn, self._job_id, self._fired = conn, job_id, False

    def execute(self, sql, *args):
        cur = self._conn.execute(sql, *args)
        if not self._fired and sql.lstrip().upper().startswith("SELECT"):
            self._fired = True
            with db.get_conn() as web:  # the web process handling POST /hide
                web.execute("UPDATE jobs SET hidden = 1 WHERE id = ?",
                            (self._job_id,))
        return cur


def test_hide_between_select_and_claim_loses_the_race(fresh_db):
    """sqlite3 opens no transaction for a SELECT and WAL readers don't block
    writers, so a hide can land mid-claim. The worker must notice and drop
    the job rather than solve something already pulled from the site."""
    with db.get_conn() as conn:
        _insert(conn, "bad", status="queued")

    conn = db.get_conn()
    assert worker.claim_next_job(_HideMidClaim(conn, "bad")) is None
    conn.commit()

    with db.get_conn() as check:
        row = check.execute("SELECT status, hidden FROM jobs WHERE id='bad'").fetchone()
    # never claimed: still queued (for the sweep to collect), still hidden
    assert (row["status"], row["hidden"]) == ("queued", 1)


def test_claim_is_atomic_against_a_lost_row(fresh_db):
    """The same re-check covers a row that stops being claimable for any
    reason between the SELECT and the UPDATE, not just a hide."""
    with db.get_conn() as conn:
        _insert(conn, "job", status="queued")

    class _StealMidClaim(_HideMidClaim):
        def execute(self, sql, *args):
            cur = self._conn.execute(sql, *args)
            if not self._fired and sql.lstrip().upper().startswith("SELECT"):
                self._fired = True
                with db.get_conn() as other:
                    other.execute("UPDATE jobs SET status = 'solving' "
                                  "WHERE id = ?", (self._job_id,))
            return cur

    conn = db.get_conn()
    assert worker.claim_next_job(_StealMidClaim(conn, "job")) is None
    conn.commit()


def test_claim_returns_the_job_when_nothing_interferes(fresh_db):
    with db.get_conn() as conn:
        _insert(conn, "job", status="queued")
        job = worker.claim_next_job(conn)
    assert job["id"] == "job"
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT status FROM jobs WHERE id='job'").fetchone()["status"] == "solving"


def test_claim_returns_none_on_an_empty_queue(fresh_db):
    with db.get_conn() as conn:
        assert worker.claim_next_job(conn) is None


def test_hidden_jobs_do_not_occupy_queue_slots(fresh_db, admin):
    """A hidden job the worker will never claim must not count against the
    queue-full gate or inflate anyone's reported position."""
    with db.get_conn() as conn:
        _insert(conn, "bad", status="queued", created_at="2026-08-13 20:00:00")
        _insert(conn, "mine", status="queued", created_at="2026-08-13 21:00:00")
    assert main._queue_depth() == 2
    assert main.get_job("mine")["queue_position"] == 1

    main.hide_job("bad", _auth())

    assert main._queue_depth() == 1
    assert main.get_job("mine")["queue_position"] == 0


def test_sweep_still_collects_hidden_jobs(fresh_db, admin, tmp_path):
    """Hiding buys time; the retention sweep is still what deletes the bytes."""
    upload = tmp_path / "old.jpg"
    upload.write_bytes(b"x")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, image_path, hidden, created_at) VALUES "
            "('bad', ?, 1, datetime('now', '-25 hours'))", (str(upload),))

    assert worker.sweep_expired() == 1
    assert not upload.exists()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
