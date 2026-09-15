"""Visitor feedback as GitHub issues (#137): the guards on an open endpoint
that writes to a public repository, and the shape of what it files. GitHub
is stood in for; no network."""

import json

import pytest
from fastapi.testclient import TestClient

from app import db, feedback, main
from tests.test_moderation import _insert, fresh_db  # noqa: F401


@pytest.fixture()
def github(monkeypatch):
    """A configured token, a captured payload, and fresh limiter state."""
    box = []

    def fake_post(payload):
        box.append(payload)
        return {"number": 7, "html_url": "https://github.com/cwage/asterism/issues/7"}

    monkeypatch.setattr(feedback, "TOKEN", "ghp_test")
    monkeypatch.setattr(feedback, "REPO", "cwage/asterism")
    monkeypatch.setattr(feedback, "_post", fake_post)
    monkeypatch.setattr(main, "_feedback_log", main._feedback_log.__class__(main._feedback_log.default_factory))
    return box


def _send(client, description, context=None, ip="203.0.113.7"):
    return client.post("/feedback", json={"description": description, "context": context},
                       headers={"fly-client-ip": ip, "user-agent": "TestBrowser/1.0"})


def test_a_report_is_filed_with_three_fenced_sections(fresh_db, github):
    with db.get_conn() as conn:
        _insert(conn, "a" * 32, status="failed",
                result={"failure": {"reason": "no_match"}, "attempts": [1, 2],
                        "fov_bounds": [30, 60]})
        conn.execute("UPDATE jobs SET error = 'no solution at 21-41deg' WHERE id = ?", ("a" * 32,))
    client = TestClient(main.app)
    resp = _send(client, "The labels are off by a bit\nsecond line",
                 {"job": "a" * 32, "url": "https://asterism.quietlife.net/?job=" + "a" * 32})
    assert resp.status_code == 200
    assert resp.json() == {"number": 7, "url": "https://github.com/cwage/asterism/issues/7"}
    (payload,) = github
    assert payload["title"] == "Feedback: The labels are off by a bit"
    assert payload["labels"] == ["bug-report"]
    body = payload["body"]
    assert body.count("```") == 6
    assert "### What they wrote" in body and "second line" in body
    assert '"job": "' + "a" * 32 in body            # the browser's context
    assert '"status": "failed"' in body               # the server's view of that job
    assert '"failure": "no_match"' in body
    assert '"attempts": 2' in body
    assert '"user_agent": "TestBrowser/1.0"' in body
    assert '"filed_at_utc"' in body


def test_mentions_and_fences_in_the_text_are_neutralised(fresh_db, github):
    client = TestClient(main.app)
    resp = _send(client, "hey @octocat look ```\n# not a heading\n```\nand ```again```",
                 {"note": "cc @someone"})
    assert resp.status_code == 200
    body = github[0]["body"]
    assert "@octocat" not in body and "＠octocat" in body
    assert "@someone" not in body and "＠someone" in body
    # the visitor's own backticks, however many runs, can't close the fence
    assert body.count("```") == 6
    # nor does the title carry a mention or a backtick
    assert github[0]["title"] == "Feedback: hey ＠octocat look '''"


def test_no_token_means_no_endpoint(fresh_db, github, monkeypatch):
    monkeypatch.setattr(feedback, "TOKEN", "")
    client = TestClient(main.app)
    assert _send(client, "anything").status_code == 404


def test_an_empty_report_is_refused(fresh_db, github):
    client = TestClient(main.app)
    assert _send(client, "   ").status_code == 400
    assert client.post("/feedback", data="not json",
                       headers={"content-type": "application/json"}).status_code == 400
    assert not github


def test_text_is_capped_not_refused(fresh_db, github):
    client = TestClient(main.app)
    assert _send(client, "x" * 5000, {"blob": "y" * 9000}).status_code == 200
    body = github[0]["body"]
    assert "x" * 2000 in body and "x" * 2001 not in body
    assert "(truncated)" in body


def test_one_report_a_minute_per_address_and_a_daily_cap(fresh_db, github, monkeypatch):
    client = TestClient(main.app)
    assert _send(client, "first").status_code == 200
    assert _send(client, "second").status_code == 429
    assert _send(client, "elsewhere", ip="198.51.100.9").status_code == 200
    monkeypatch.setattr(feedback, "PER_DAY", 2)
    assert _send(client, "third", ip="192.0.2.1").status_code == 429
    assert len(github) == 2
    # the cap lives in the database: a restarted process sees the same day
    with db.get_conn() as conn:
        assert feedback.filed_today(conn) == 2
        assert feedback.reserve(conn) is None


def test_a_slot_is_reserved_before_posting_and_released_on_failure(fresh_db, github, monkeypatch):
    seen = []

    def post_and_look(payload):
        with db.get_conn() as conn:
            seen.append(feedback.filed_today(conn))  # the slot is already taken
        raise RuntimeError("github down")
    monkeypatch.setattr(feedback, "_post", post_and_look)
    client = TestClient(main.app)
    assert _send(client, "one").status_code == 502
    assert seen == [1]
    with db.get_conn() as conn:
        assert feedback.filed_today(conn) == 0  # and given back


def test_the_body_is_bounded_before_it_is_parsed(fresh_db, github):
    client = TestClient(main.app)
    huge = {"description": "hi", "context": {"blob": "z" * (feedback.MAX_BODY_BYTES * 4)}}
    resp = client.post("/feedback", json=huge, headers={"fly-client-ip": "203.0.113.7"})
    assert resp.status_code == 413
    assert not github


def test_an_upstream_failure_is_a_generic_502(fresh_db, github, monkeypatch, capsys):
    def refuse(payload):
        raise RuntimeError("secret token ghp_test inside")
    monkeypatch.setattr(feedback, "_post", refuse)
    client = TestClient(main.app)
    resp = _send(client, "something")
    assert resp.status_code == 502
    assert "ghp_test" not in resp.text
    out = capsys.readouterr().out
    assert "ghp_test" not in out and "<token>" in out  # the reason is logged, redacted
    with db.get_conn() as conn:
        assert feedback.filed_today(conn) == 0  # a failure spends nothing


def test_a_job_the_page_names_but_the_server_lacks_is_said_so(fresh_db, github):
    client = TestClient(main.app)
    assert _send(client, "gone", {"job": "b" * 32}).status_code == 200
    assert '"status": "no such job"' in github[0]["body"]


def test_a_hidden_job_is_no_such_job_here_too(fresh_db, github):
    # A takedown must not be copyable into a public issue by its id.
    with db.get_conn() as conn:
        _insert(conn, "c" * 32, status="done", result={"match": {"logodds": 99}})
        conn.execute("UPDATE jobs SET hidden = 1, error = 'sensitive' WHERE id = ?", ("c" * 32,))
    client = TestClient(main.app)
    assert _send(client, "about that one", {"job": "c" * 32}).status_code == 200
    body = github[0]["body"]
    assert '"status": "no such job"' in body
    assert "sensitive" not in body and "logodds" not in body
    # and junk in the job field is ignored rather than queried
    assert _send(client, "junk", {"job": "../../etc"}, ip="198.51.100.9").status_code == 200
    assert '"job"' not in github[1]["body"].split("### From the server")[1]


def test_title_is_the_first_line_trimmed():
    assert feedback.title_for("Short and sweet\nmore") == "Feedback: Short and sweet"
    long = feedback.title_for("w" * 200)
    assert long.startswith("Feedback: ") and len(long) <= len("Feedback: ") + 80
    assert feedback.title_for("  \n ") == "Feedback: Feedback"
