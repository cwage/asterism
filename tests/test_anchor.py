"""Placing a failed job from two objects the uploader pointed at (#85)."""

import asyncio
import json

import pytest
from fastapi import HTTPException

from app import db, ephemeris, main, register, worker


class _Request:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if self._payload is _BAD:
            raise ValueError("not json")
        return self._payload


_BAD = object()

EXIF = {"width": 4000, "height": 3000, "datetime_original": "2026:08:14 20:28:00",
        "offset_time_original": "-05:00", "fov_bounds": (25.8, 88.5)}
GOOD = [{"name": "Moon", "x": 3040, "y": 1640},
        {"name": "Venus", "x": 1570, "y": 860}]


@pytest.fixture()
def failed_job(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, image_path, exif_json, mode) "
            "VALUES ('j1', 'failed', '/photos/x.jpg', ?, 'quick')",
            (json.dumps(EXIF),))
    return "j1"


def _post(job_id, payload):
    return asyncio.run(main.anchor_job(job_id, _Request(payload)))


def test_two_anchors_requeue_the_job(failed_job):
    out = _post("j1", {"anchors": GOOD})
    assert out["mode"] == "anchors"
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, mode, anchors_json FROM jobs "
                           "WHERE id = 'j1'").fetchone()
    assert (row["status"], row["mode"]) == ("queued", "anchors")
    assert [a["name"] for a in json.loads(row["anchors_json"])] == ["Moon", "Venus"]


def test_anchors_outside_the_photo_are_refused(failed_job):
    bad = [{"name": "Moon", "x": 9000, "y": 100}, GOOD[1]]
    with pytest.raises(HTTPException) as err:
        _post("j1", {"anchors": bad})
    assert err.value.status_code == 400


def test_the_same_object_twice_is_refused(failed_job):
    # Two taps on the Moon fix nothing: the pair has to span a known angle.
    same = [dict(GOOD[0]), dict(GOOD[0], x=1000, y=1000)]
    with pytest.raises(HTTPException) as err:
        _post("j1", {"anchors": same})
    assert err.value.status_code == 400


def test_unknown_object_names_are_refused(failed_job):
    with pytest.raises(HTTPException) as err:
        _post("j1", {"anchors": [{"name": "Ganymede", "x": 10, "y": 10}, GOOD[1]]})
    assert err.value.status_code == 400


@pytest.mark.parametrize("payload", [
    {"anchors": [GOOD[0]]},                    # one is not enough
    {"anchors": GOOD + [dict(GOOD[0])]},       # three is not the contract
    {"anchors": "moon"},
    {},
    _BAD,
], ids=["one", "three", "not-a-list", "empty", "not-json"])
def test_malformed_requests_are_refused(failed_job, payload):
    with pytest.raises(HTTPException) as err:
        _post("j1", payload)
    assert err.value.status_code == 400


def test_a_solved_job_cannot_be_placed_by_hand(failed_job):
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = 'j1'")
    with pytest.raises(HTTPException) as err:
        _post("j1", {"anchors": GOOD})
    assert err.value.status_code == 409


def test_hidden_jobs_are_indistinguishable_from_missing(failed_job):
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET hidden = 1 WHERE id = 'j1'")
    with pytest.raises(HTTPException) as err:
        _post("j1", {"anchors": GOOD})
    assert err.value.status_code == 404


def test_worker_refuses_without_a_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    job = {"id": "j2", "image_path": "/photos/x.jpg", "mode": "anchors",
           "anchors_json": json.dumps(GOOD),
           "exif_json": json.dumps({"width": 4000, "height": 3000})}
    status, result, error = worker.process(job)
    assert status == "failed"
    assert result["failure"]["reason"] == "no_anchor_time"
    assert "timestamp" in error


def test_taps_snap_to_the_nearest_detected_source(tmp_path):
    """A thumb is not a centroid: taps land tens of pixels off and still have
    to produce the same registration."""
    from tests import synth
    path = tmp_path / "sky.jpg"
    synth.render_points(str(path), [(1200, 900), (2600, 1800)], width=4000,
                        height=3000, amps=[220.0, 200.0])
    bodies = [{"name": "Moon", "ra": 190.0, "dec": -5.0, "kind": "moon"},
              {"name": "Venus", "ra": 176.0, "dec": 2.0, "kind": "planet"}]
    sloppy = [{"name": "Moon", "x": 1255, "y": 845},
              {"name": "Venus", "x": 2550, "y": 1860}]
    out = register.register_from_anchors(str(path), {"width": 4000, "height": 3000},
                                         sloppy, bodies)
    assert out is not None
    placed = {a["name"]: a for a in out["anchors"]}
    assert placed["Moon"]["snapped"] and placed["Venus"]["snapped"]
    assert abs(placed["Moon"]["x"] - 1200) < 15
    assert abs(placed["Venus"]["y"] - 1800) < 15


def test_a_tap_on_nothing_is_used_as_given(tmp_path):
    # Empty sky under the finger: trust the person, who can see the object,
    # over a detector that evidently cannot.
    from tests import synth
    path = tmp_path / "empty.jpg"
    synth.render_points(str(path), [(1200, 900)], width=4000, height=3000,
                        amps=[220.0])
    bodies = [{"name": "Moon", "ra": 190.0, "dec": -5.0, "kind": "moon"},
              {"name": "Venus", "ra": 176.0, "dec": 2.0, "kind": "planet"}]
    anchors = [{"name": "Moon", "x": 1200, "y": 900},
               {"name": "Venus", "x": 3000, "y": 2000}]
    out = register.register_from_anchors(str(path), {"width": 4000, "height": 3000},
                                         anchors, bodies)
    assert out is not None
    placed = {a["name"]: a for a in out["anchors"]}
    assert placed["Venus"]["snapped"] is False
    assert (placed["Venus"]["x"], placed["Venus"]["y"]) == (3000.0, 2000.0)


def test_each_anchor_reports_how_far_the_snap_moved_it(tmp_path):
    """The baseline between the anchors sets both scale and roll, so how far
    the snap dragged one is the single number that says whether the fit can
    be believed. On a real job a 110px snap on a 447px baseline was a 25%
    scale error and 3.6 degrees of roll — because the object being pointed
    at was not visible in the frame at all, and the snap found a peak
    anyway."""
    from tests import synth
    path = tmp_path / "sky.jpg"
    synth.render_points(str(path), [(1200, 900), (2600, 1800)], width=4000,
                        height=3000, amps=[220.0, 200.0])
    bodies = [{"name": "Moon", "ra": 190.0, "dec": -5.0, "kind": "moon"},
              {"name": "Venus", "ra": 176.0, "dec": 2.0, "kind": "planet"}]
    anchors = [{"name": "Moon", "x": 1200, "y": 900},   # dead on
               {"name": "Venus", "x": 2660, "y": 1800}]  # 60px away
    out = register.register_from_anchors(str(path), {"width": 4000, "height": 3000},
                                         anchors, bodies)
    placed = {a["name"]: a for a in out["anchors"]}
    assert placed["Moon"]["moved_px"] < 5
    assert 50 < placed["Venus"]["moved_px"] < 70


def test_an_unsnapped_anchor_moved_nothing(tmp_path):
    from tests import synth
    path = tmp_path / "empty.jpg"
    synth.render_points(str(path), [(1200, 900)], width=4000, height=3000,
                        amps=[220.0])
    bodies = [{"name": "Moon", "ra": 190.0, "dec": -5.0, "kind": "moon"},
              {"name": "Venus", "ra": 176.0, "dec": 2.0, "kind": "planet"}]
    anchors = [{"name": "Moon", "x": 1200, "y": 900},
               {"name": "Venus", "x": 3000, "y": 2000}]
    out = register.register_from_anchors(str(path), {"width": 4000, "height": 3000},
                                         anchors, bodies)
    placed = {a["name"]: a for a in out["anchors"]}
    assert placed["Venus"]["snapped"] is False
    assert placed["Venus"]["moved_px"] == 0.0
