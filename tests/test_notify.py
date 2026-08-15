"""Activity notifications (#69): the nightly summary, the burst alert, and
the watermarks that stop either one repeating. No network in the fast tier —
every test replaces notify.post.
"""

import pytest

from app import db, notify
from tests.test_moderation import _insert, fresh_db  # noqa: F401


@pytest.fixture()
def sent(monkeypatch):
    """Capture publishes instead of making them, and report success."""
    box = []

    def fake_post(message, title=None, tags=None, priority=None):
        box.append({"message": message, "title": title, "priority": priority})
        return True

    monkeypatch.setattr(notify, "post", fake_post)
    monkeypatch.setattr(notify, "TOPIC_URL", "https://ntfy.example/topic")
    return box


def _at(conn, offset):
    """A timestamp `offset` (a SQLite modifier) from now, in the same format
    and from the same clock as created_at."""
    return conn.execute("SELECT datetime('now', ?)", (offset,)).fetchone()[0]


# --- counts -----------------------------------------------------------


def test_counts_split_solves_failures_and_reasons(fresh_db):
    with db.get_conn() as conn:
        recent = _at(conn, "-1 hours")
        _insert(conn, "a", status="done", created_at=recent)
        _insert(conn, "b", status="done", created_at=recent)
        _insert(conn, "c", status="failed", created_at=recent,
                result={"failure": {"reason": "no_stars"}})
        _insert(conn, "d", status="failed", created_at=recent,
                result={"failure": {"reason": "no_stars"}})
        _insert(conn, "e", status="failed", created_at=recent,
                result={"failure": {"reason": "no_match"}})
        # Outside the window, and so not counted.
        _insert(conn, "old", status="done", created_at="2020-01-01 00:00:00")
        counts = notify.activity_counts(conn, _at(conn, "-24 hours"))

    assert counts["uploads"] == 5
    assert counts["solved"] == 2
    assert counts["failed"] == 3
    assert counts["reasons"] == {"no_stars": 2, "no_match": 1}


def test_featured_is_a_running_total_not_a_window_count(fresh_db):
    """The whole point of the flag is outliving the window, so counting it
    inside one would always report zero for the jobs that matter."""
    with db.get_conn() as conn:
        _insert(conn, "old", status="done", created_at="2020-01-01 00:00:00")
        conn.execute("UPDATE jobs SET featured = 1 WHERE id = 'old'")
        _insert(conn, "new", status="done", created_at=_at(conn, "-1 hours"))
        counts = notify.activity_counts(conn, _at(conn, "-24 hours"))

    assert counts["uploads"] == 1  # the old one is outside the window
    assert counts["featured"] == 1  # but still featured


def test_a_malformed_result_does_not_break_the_count(fresh_db):
    with db.get_conn() as conn:
        recent = _at(conn, "-1 hours")
        _insert(conn, "a", status="failed", created_at=recent)
        conn.execute("UPDATE jobs SET result_json = 'not json' WHERE id = 'a'")
        counts = notify.activity_counts(conn, _at(conn, "-24 hours"))

    assert counts["failed"] == 1
    assert counts["reasons"] == {}


def test_summary_reads_as_a_sentence():
    line = notify.format_summary({
        "uploads": 47, "solved": 39, "failed": 8, "hidden": 2, "featured": 10,
        "reasons": {"no_stars": 6, "no_match": 2},
    })
    assert line == ("47 uploads · 39 solved · 8 failed (6 no_stars, 2 no_match)"
                    " · 2 hidden · 10 featured")


# --- burst ------------------------------------------------------------


def test_burst_fires_once_and_then_holds(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(notify, "BURST_SOLVES", 3)
    with db.get_conn() as conn:
        recent = _at(conn, "-10 minutes")
        for i in range(4):
            _insert(conn, f"j{i}", status="done", created_at=recent)

        assert notify.check_burst(conn, notify._utc_now(conn)) is not None
        assert len(sent) == 1
        assert "4 solves" in sent[0]["message"]

        # Same four solves on the next tick: the watermark has moved past
        # them, so there is nothing new to say.
        assert notify.check_burst(conn, notify._utc_now(conn)) is None
        assert len(sent) == 1


def test_burst_counts_again_after_the_watermark(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(notify, "BURST_SOLVES", 3)
    with db.get_conn() as conn:
        for i in range(3):
            _insert(conn, f"first{i}", status="done",
                    created_at="2026-08-15 10:00:00")
        assert notify.check_burst(conn, "2026-08-15 10:10:00") is not None
        assert len(sent) == 1

        # A second wave, after the alert. The first three are still inside
        # the hour window, but the watermark excludes them — so the count
        # is 3, not 6.
        for i in range(3):
            _insert(conn, f"second{i}", status="done",
                    created_at="2026-08-15 10:20:00")
        assert notify.check_burst(conn, "2026-08-15 10:30:00") is not None
        assert len(sent) == 2
        assert "3 solves" in sent[1]["message"]


def test_burst_ignores_failures_and_quiet_hours(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(notify, "BURST_SOLVES", 3)
    with db.get_conn() as conn:
        recent = _at(conn, "-10 minutes")
        for i in range(5):
            _insert(conn, f"f{i}", status="failed", created_at=recent)
        # Solves, but from before the window.
        for i in range(5):
            _insert(conn, f"o{i}", status="done", created_at=_at(conn, "-5 hours"))
        assert notify.check_burst(conn, notify._utc_now(conn)) is None
    assert sent == []


def test_a_failed_publish_leaves_the_burst_still_reportable(fresh_db, monkeypatch):
    """If ntfy was down, the next tick has to be able to try again — so the
    watermark advances on delivery, not on the attempt."""
    monkeypatch.setattr(notify, "BURST_SOLVES", 2)
    monkeypatch.setattr(notify, "TOPIC_URL", "https://ntfy.example/topic")
    attempts = []
    monkeypatch.setattr(notify, "post",
                        lambda *a, **k: (attempts.append(1), False)[1])
    with db.get_conn() as conn:
        for i in range(3):
            _insert(conn, f"j{i}", status="done", created_at=_at(conn, "-5 minutes"))
        notify.check_burst(conn, notify._utc_now(conn))
        notify.check_burst(conn, notify._utc_now(conn))
    assert len(attempts) == 2


# --- summary ----------------------------------------------------------


def test_summary_waits_for_its_hour(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(notify, "SUMMARY_HOUR_UTC", 7)
    with db.get_conn() as conn:
        assert notify.check_summary(conn, "2026-08-15 06:59:00") is None
        assert sent == []
        assert notify.check_summary(conn, "2026-08-15 07:00:00") is not None
        assert len(sent) == 1


def test_summary_sends_once_per_day(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(notify, "SUMMARY_HOUR_UTC", 7)
    with db.get_conn() as conn:
        notify.check_summary(conn, "2026-08-15 07:00:00")
        # Every later tick that same day is a no-op...
        assert notify.check_summary(conn, "2026-08-15 09:00:00") is None
        assert notify.check_summary(conn, "2026-08-15 23:00:00") is None
        assert len(sent) == 1
        # ...and the next day starts again.
        assert notify.check_summary(conn, "2026-08-16 07:30:00") is not None
        assert len(sent) == 2


def test_a_late_summary_still_goes_out(fresh_db, sent, monkeypatch):
    """The machine stops on an idle night, so the first tick after a wake
    can be hours past the hour. Late is the intended behaviour, not a miss."""
    monkeypatch.setattr(notify, "SUMMARY_HOUR_UTC", 7)
    with db.get_conn() as conn:
        assert notify.check_summary(conn, "2026-08-15 19:42:00") is not None
    assert len(sent) == 1


# --- wiring -----------------------------------------------------------


def test_disabled_without_a_topic_url(fresh_db, monkeypatch):
    monkeypatch.setattr(notify, "TOPIC_URL", "")
    with db.get_conn() as conn:
        for i in range(50):
            _insert(conn, f"j{i}", status="done", created_at=_at(conn, "-1 minutes"))
        assert notify.enabled() is False
        assert notify.tick(conn) == []
        # And nothing was recorded, so switching it on later starts clean.
        assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0


def test_a_broken_check_does_not_take_down_the_tick(fresh_db, sent, monkeypatch):
    """Solving photos is the job. A notification failing is not worth
    failing that for, so tick swallows and carries on."""
    def boom(conn, now):
        raise RuntimeError("watermark table on fire")

    monkeypatch.setattr(notify, "check_burst", boom)
    monkeypatch.setattr(notify, "SUMMARY_HOUR_UTC", 0)
    with db.get_conn() as conn:
        assert notify.tick(conn) != []  # the summary still went out
    assert len(sent) == 1


def test_post_never_raises_on_a_dead_endpoint(monkeypatch):
    monkeypatch.setattr(notify, "TOPIC_URL", "http://127.0.0.1:1/nope")
    monkeypatch.setattr(notify, "TIMEOUT_SECONDS", 0.5)
    assert notify.post("hello") is False
