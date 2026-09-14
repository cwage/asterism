"""Public "recently solved" feed: done jobs only, newest first, capped,
with the narration caption riding along when the worker produced one.
Served twice: as JSON for the homepage strip and as Atom (#127) for
feed readers."""

import json
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import db, main

ATOM = {"a": main.ATOM_NS}


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


def _atom(base_url="http://testserver"):
    resp = TestClient(main.app, base_url=base_url).get("/feed.atom")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/atom+xml")
    return ET.fromstring(resp.content)


def test_atom_feed_mirrors_the_strip(fresh_db):
    with db.get_conn() as conn:
        _insert(conn, "older", "done", "2026-08-13 21:00:00",
                {"labels": []})
        _insert(conn, "newer", "done", "2026-08-13 22:00:00",
                {"labels": [], "narration": {"caption": "Jupiter <rising>",
                                             "text": "A bright & steady dot.",
                                             "model": "m"}})
        _insert(conn, "nope1", "failed", "2026-08-13 23:00:00")
        _insert(conn, "nope2", "queued", "2026-08-13 23:00:00")
        _insert(conn, "gone", "done", "2026-08-13 23:30:00", {"labels": []})
        conn.execute("UPDATE jobs SET hidden = 1 WHERE id = 'gone'")

    feed = _atom("https://asterism.example")
    assert feed.findtext("a:title", namespaces=ATOM) == main.ATOM_TITLE
    # dated by the newest entry
    assert feed.findtext("a:updated", namespaces=ATOM) == "2026-08-13T22:00:00Z"
    # links are absolute, built from the request's own origin
    assert (feed.find("a:link[@rel='self']", ATOM).get("href")
            == "https://asterism.example/feed.atom")
    assert (feed.find("a:link[@rel='alternate']", ATOM).get("href")
            == "https://asterism.example/")

    entries = feed.findall("a:entry", ATOM)
    # same membership and order as /feed: no failed, queued, or hidden jobs
    assert [e.findtext("a:title", namespaces=ATOM) for e in entries] == [
        "Jupiter <rising>", main.ATOM_UNTITLED]
    newer = entries[0]
    result_url = "https://asterism.example/?job=newer"
    assert newer.findtext("a:id", namespaces=ATOM) == result_url
    assert newer.find("a:link[@rel='alternate']", ATOM).get("href") == result_url
    enclosure = newer.find("a:link[@rel='enclosure']", ATOM)
    assert enclosure.get("href") == "https://asterism.example/jobs/newer/card"
    assert enclosure.get("type") == "image/png"
    assert newer.findtext("a:published", namespaces=ATOM) == "2026-08-13T22:00:00Z"
    assert newer.findtext("a:updated", namespaces=ATOM) == "2026-08-13T22:00:00Z"
    content = newer.find("a:content", ATOM)
    assert content.get("type") == "html"
    assert 'src="https://asterism.example/jobs/newer/card"' in content.text
    # narration and caption are HTML-escaped inside the html content
    assert "Jupiter &lt;rising&gt;" in content.text
    assert "A bright &amp; steady dot." in content.text
    # no narration: the card alone, no empty paragraph after it
    older = entries[1].findtext("a:content", namespaces=ATOM)
    assert "/jobs/older/card" in older
    assert older.count("<p>") == 1


def test_atom_feed_is_capped_like_the_strip(fresh_db):
    with db.get_conn() as conn:
        for i in range(main.FEED_LIMIT + 5):
            _insert(conn, f"job{i:03}", "done", f"2026-08-13 10:{i:02}:00")
    entries = _atom().findall("a:entry", ATOM)
    assert len(entries) == main.FEED_LIMIT
    assert entries[0].findtext("a:id", namespaces=ATOM).endswith(
        f"?job=job{main.FEED_LIMIT + 4:03}")


def test_atom_feed_empty_is_still_a_feed(fresh_db):
    feed = _atom()
    assert feed.findall("a:entry", ATOM) == []
    # Atom requires <updated> at feed level even when there is nothing in it
    assert feed.findtext("a:updated", namespaces=ATOM).endswith("Z")
    assert feed.find("a:author/a:name", ATOM).text == "asterism"


def test_get_routes_answer_head(fresh_db):
    """Readers HEAD an enclosure for its size and link checkers HEAD result
    pages; FastAPI's default is a 405 for every one of them."""
    client = TestClient(main.app)
    for path in ("/feed.atom", "/feed", "/"):
        head = client.head(path)
        assert head.status_code == 200, path
        # Same headers as the GET. The body itself is dropped by uvicorn,
        # not by the app, so the test client still sees one here.
        assert head.headers["content-length"] == client.get(path).headers["content-length"]
    # the route matches, so it is the job that is missing, not the method
    assert client.head("/jobs/nope/card").status_code == 404
    assert client.head("/jobs/nope").status_code == 404
    # nothing has been opened up that was not a GET
    assert client.head("/jobs").status_code == 405
