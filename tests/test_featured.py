"""Featured flag (#67): operator-chosen solves survive the retention sweep so
the feed has something in it on a quiet day. Plus the undo endpoints for both
this and the kill switch (#60), which previously needed ssh and raw SQL."""

import pytest
from fastapi import HTTPException

from app import db, main, worker
from tests.test_moderation import _Req, _auth, _insert, admin, fresh_db  # noqa: F401


def _old(conn, job_id, **kw):
    """A job already past the retention window."""
    _insert(conn, job_id, created_at="2020-01-01 00:00:00", **kw)


def test_featured_job_survives_the_sweep(fresh_db, admin, tmp_path):
    keep = tmp_path / "keep.jpg"
    drop = tmp_path / "drop.jpg"
    keep.write_bytes(b"x")
    drop.write_bytes(b"x")
    with db.get_conn() as conn:
        _old(conn, "keep", image_path=str(keep))
        _old(conn, "drop", image_path=str(drop))

    main.feature_job("keep", _auth())

    assert worker.sweep_expired() == 1  # only the unfeatured one
    assert keep.exists()
    assert not drop.exists()
    with db.get_conn() as conn:
        assert [r["id"] for r in conn.execute("SELECT id FROM jobs")] == ["keep"]


def test_featured_job_survives_repeated_sweeps(fresh_db, admin, tmp_path):
    """The exemption is a property of the row, not a one-time reprieve."""
    img = tmp_path / "keep.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _old(conn, "keep", image_path=str(img))
    main.feature_job("keep", _auth())

    for _ in range(3):
        assert worker.sweep_expired() == 0
    assert img.exists()


def test_unfeature_lets_the_sweep_collect_it(fresh_db, admin, tmp_path):
    img = tmp_path / "keep.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _old(conn, "keep", image_path=str(img))
    main.feature_job("keep", _auth())
    assert worker.sweep_expired() == 0

    assert main.unfeature_job("keep", _auth()) == {"id": "keep", "featured": False}

    assert worker.sweep_expired() == 1
    assert not img.exists()


def test_hiding_clears_featured(fresh_db, admin, tmp_path):
    """The kill switch outranks the showcase. Both flags set at once would
    strand a job that is invisible *and* exempt from the sweep, so its bytes
    would never leave the disk — the opposite of what hiding is for."""
    img = tmp_path / "bad.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _old(conn, "bad", image_path=str(img))
    main.feature_job("bad", _auth())

    main.hide_job("bad", _auth())

    with db.get_conn() as conn:
        row = conn.execute("SELECT hidden, featured FROM jobs WHERE id='bad'").fetchone()
    assert (row["hidden"], row["featured"]) == (1, 0)
    # and it is collectable again
    assert worker.sweep_expired() == 1
    assert not img.exists()


def test_featuring_a_hidden_job_is_refused(fresh_db, admin):
    """The other direction of the same rule."""
    with db.get_conn() as conn:
        _insert(conn, "bad")
    main.hide_job("bad", _auth())

    with pytest.raises(HTTPException) as e:
        main.feature_job("bad", _auth())
    assert e.value.status_code == 409

    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT featured FROM jobs WHERE id='bad'").fetchone()["featured"] == 0


def test_only_solved_jobs_can_be_featured(fresh_db, admin):
    with db.get_conn() as conn:
        _insert(conn, "failed", status="failed")
        _insert(conn, "queued", status="queued")
    for job_id in ("failed", "queued"):
        with pytest.raises(HTTPException) as e:
            main.feature_job(job_id, _auth())
        assert e.value.status_code == 409


def test_unhide_restores_every_read_path(fresh_db, admin, tmp_path):
    img = tmp_path / "oops.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _insert(conn, "oops", image_path=str(img), result={"labels": []})
    main.hide_job("oops", _auth())
    with pytest.raises(HTTPException):
        main.get_job("oops")

    assert main.unhide_job("oops", _auth()) == {"id": "oops", "hidden": False}

    assert main.get_job("oops")["status"] == "done"
    assert [j["id"] for j in main.feed()["jobs"]] == ["oops"]
    assert main.get_job_image("oops") is not None


def test_unhide_does_not_restore_featured(fresh_db, admin):
    """Hiding clears the flag; unhiding is not a time machine. Re-featuring is
    a deliberate second decision."""
    with db.get_conn() as conn:
        _insert(conn, "job")
    main.feature_job("job", _auth())
    main.hide_job("job", _auth())
    main.unhide_job("job", _auth())

    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT featured FROM jobs WHERE id='job'").fetchone()["featured"] == 0


def test_admin_endpoints_reject_bad_tokens(fresh_db, admin):
    with db.get_conn() as conn:
        _insert(conn, "job")
    calls = (main.feature_job, main.unfeature_job, main.unhide_job)
    for call in calls:
        for request in (_Req(), _Req("Bearer wrong"), _Req("Basic s3cret")):
            with pytest.raises(HTTPException) as e:
                call("job", request)
            assert e.value.status_code == 404
    with db.get_conn() as conn:
        row = conn.execute("SELECT hidden, featured FROM jobs WHERE id='job'").fetchone()
    assert (row["hidden"], row["featured"]) == (0, 0)


def test_admin_endpoints_absent_without_a_token(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    with db.get_conn() as conn:
        _insert(conn, "job")
    for call in (main.feature_job, main.unfeature_job, main.unhide_job):
        with pytest.raises(HTTPException) as e:
            call("job", _auth())
        assert e.value.status_code == 404


def test_admin_endpoints_404_unknown_jobs(fresh_db, admin):
    for call in (main.feature_job, main.unfeature_job, main.unhide_job):
        with pytest.raises(HTTPException) as e:
            call("nosuchjob", _auth())
        assert e.value.status_code == 404


class _FeatureMidSweep:
    """Connection wrapper that commits a /feature the instant the sweep's
    SELECT runs — the interleaving the conditional DELETE defends against."""

    def __init__(self, conn, job_id):
        self._conn, self._job_id, self._fired = conn, job_id, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._conn.commit()
        return False

    def execute(self, sql, *args):
        cur = self._conn.execute(sql, *args)
        if not self._fired and sql.lstrip().upper().startswith("SELECT"):
            self._fired = True
            with db.get_conn() as web:  # the web process handling /feature
                web.execute("UPDATE jobs SET featured = 1 WHERE id = ?",
                            (self._job_id,))
        return cur


def test_feature_between_select_and_delete_is_honoured(fresh_db, monkeypatch,
                                                       tmp_path):
    """Featuring a job while the sweep is mid-pass must save it. sqlite3 opens
    no transaction for the SELECT, and unlink() has nothing to roll back, so
    deleting bytes before re-checking the flag would destroy a job that had
    just been marked permanent."""
    img = tmp_path / "keep.jpg"
    img.write_bytes(b"x")
    with db.get_conn() as conn:
        _old(conn, "keep", image_path=str(img))

    real_get_conn = db.get_conn
    wrapped = {"done": False}

    def fake_get_conn():
        conn = real_get_conn()
        if wrapped["done"]:
            return conn
        wrapped["done"] = True
        return _FeatureMidSweep(conn, "keep")

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    try:
        assert worker.sweep_expired() == 0
    finally:
        monkeypatch.setattr(db, "get_conn", real_get_conn)

    assert img.exists(), "the bytes of a job featured mid-sweep were deleted"
    with db.get_conn() as conn:
        row = conn.execute("SELECT featured FROM jobs WHERE id='keep'").fetchone()
    assert row is not None and row["featured"] == 1


def test_sweep_reports_what_it_actually_deleted(fresh_db, admin, tmp_path):
    """The count is the number of rows taken, not the number selected — a job
    rescued mid-pass must not be counted as swept."""
    with db.get_conn() as conn:
        _old(conn, "a")
        _old(conn, "b")
    main.feature_job("a", _auth())
    assert worker.sweep_expired() == 1


class _DeleteMidCall:
    """Stands in for a connection, deleting the row the moment the endpoint's
    SELECT runs — i.e. the retention sweep landing mid-request. Doubles as its
    own context manager so it drops into `with db.get_conn() as conn`."""

    def __init__(self, conn, job_id):
        self._conn, self._job_id, self._fired = conn, job_id, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._conn.commit()
        return False

    def execute(self, sql, *args):
        cur = self._conn.execute(sql, *args)
        if not self._fired and sql.lstrip().upper().startswith("SELECT"):
            self._fired = True
            with db.get_conn() as sweeper:
                sweeper.execute("DELETE FROM jobs WHERE id = ?", (self._job_id,))
        return cur


class _HideMidFeature:
    """Commits a /hide immediately after the endpoint's first SELECT. Against
    check-then-set that lands between the guard and the write; against the
    conditional UPDATE there is no pre-write SELECT to land after, which is
    the fix."""

    def __init__(self, conn, job_id):
        self._conn, self._job_id, self._fired = conn, job_id, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._conn.commit()
        return False

    def execute(self, sql, *args):
        cur = self._conn.execute(sql, *args)
        if not self._fired and sql.lstrip().upper().startswith("SELECT"):
            self._fired = True
            with db.get_conn() as web:
                web.execute(
                    "UPDATE jobs SET hidden = 1, featured = 0 WHERE id = ?",
                    (self._job_id,))
        return cur


def test_hide_during_feature_cannot_strand_the_job(fresh_db, admin, monkeypatch):
    """hidden and featured must never both end up set. That job would be
    invisible *and* exempt from the sweep, so its bytes would never leave the
    disk — the opposite of what hiding is for."""
    with db.get_conn() as conn:
        _insert(conn, "job")

    real_get_conn = db.get_conn
    raw = real_get_conn()
    monkeypatch.setattr(db, "get_conn", lambda: _HideMidFeature(raw, "job"))
    try:
        try:
            main.feature_job("job", _auth())
        except HTTPException as e:
            assert e.status_code == 409  # lost the race cleanly
    finally:
        monkeypatch.setattr(db, "get_conn", real_get_conn)
        raw.close()

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT hidden, featured FROM jobs WHERE id='job'").fetchone()
    assert not (row["hidden"] and row["featured"]), \
        "job ended up hidden and featured: invisible and immortal"


@pytest.mark.parametrize("endpoint", ["hide_job"])
def test_admin_endpoints_404_when_the_row_vanishes(fresh_db, admin, monkeypatch,
                                                   endpoint):
    """The sweep can delete a row between an endpoint's SELECT and its UPDATE.
    Reporting success for a job that no longer exists is worse than saying
    it's gone.

    feature_job is deliberately absent: its preconditions all ride in the
    UPDATE now, so it has no pre-write SELECT for a delete to land after. The
    equivalent cases reach it through the post-failure diagnostic instead —
    see the unknown-job, hidden-job, and not-solved tests."""
    with db.get_conn() as conn:
        _insert(conn, "gone")

    real_get_conn = db.get_conn
    raw = real_get_conn()
    monkeypatch.setattr(db, "get_conn",
                        lambda: _DeleteMidCall(raw, "gone")
                        if not raw.in_transaction else real_get_conn())
    try:
        with pytest.raises(HTTPException) as e:
            getattr(main, endpoint)("gone", _auth())
        assert e.value.status_code == 404
    finally:
        monkeypatch.setattr(db, "get_conn", real_get_conn)
        raw.close()


def test_featured_job_still_appears_in_the_feed(fresh_db, admin):
    """Featuring changes retention, not visibility: it stays an ordinary feed
    entry, just one that outlives the sweep."""
    with db.get_conn() as conn:
        _old(conn, "kept", result={"labels": []})
    main.feature_job("kept", _auth())
    assert [j["id"] for j in main.feed()["jobs"]] == ["kept"]
