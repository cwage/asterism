"""Worker result assembly: star + body labels merge, ephemeris failures
never sink a successful solve. Everything external is stubbed."""

import json

import pytest

from app import constellations, db, ephemeris, solver, worker

JOB = {"id": "abc123", "image_path": "/photos/x.jpg",
       "exif_json": json.dumps({"width": 100, "height": 100})}

STARS = [{"name": "Sirius", "x": 10.0, "y": 10.0, "mag": -1.44, "kind": "star"}]
BODIES = [{"name": "Jupiter", "x": 50.0, "y": 50.0, "mag": -2.1, "kind": "planet"}]
FIGURES = [{"name": "Orion", "abbr": "Ori", "segments": [[0.0, 0.0, 10.0, 10.0]]}]


@pytest.fixture(autouse=True)
def stub_solve(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(solver, "solve_tiered", lambda *a: {
        "success": True, "wcs_path": "/fake.wcs", "total_seconds": 1.0,
        "attempts": [{"fov_bounds": [30, 90], "seconds": 1.0, "success": True}],
    })
    monkeypatch.setattr(solver, "annotate", lambda *a, **k: list(STARS))
    monkeypatch.setattr(constellations, "annotate", lambda *a: list(FIGURES))


def test_bodies_merge_ahead_of_stars(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: (list(BODIES), {"time_source": "exif_offset"}))
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["labels"] == BODIES + STARS
    assert result["ephemeris"]["time_source"] == "exif_offset"
    assert result["constellations"] == FIGURES


def test_ephemeris_crash_does_not_fail_the_job(monkeypatch):
    def boom(*a):
        raise RuntimeError("ephemeris exploded")
    monkeypatch.setattr(ephemeris, "annotate_bodies", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["labels"] == STARS
    # stable client-facing schema, no traceback leakage
    assert result["ephemeris"] == {"time_utc": None, "time_source": None,
                                   "error": "ephemeris computation failed"}


def test_constellations_crash_does_not_fail_the_job(monkeypatch):
    def boom(*a):
        raise RuntimeError("constellations exploded")
    monkeypatch.setattr(constellations, "annotate", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["labels"] == STARS  # no timestamp in JOB -> no bodies
    assert result["constellations"] == []
