"""Worker result assembly: star + body labels merge, ephemeris failures
never sink a successful solve, quick/deep gating. Everything external is
stubbed."""

import json

import pytest

from app import (constellations, db, ephemeris, narrate, satellites, solver,
                 verify, worker)

JOB = {"id": "abc123", "image_path": "/photos/x.jpg", "mode": "quick",
       "exif_json": json.dumps({"width": 100, "height": 100})}

STARS = [{"name": "Sirius", "x": 10.0, "y": 10.0, "mag": -1.44, "kind": "star"}]
BODIES = [{"name": "Jupiter", "x": 50.0, "y": 50.0, "mag": -2.1, "kind": "planet"}]
FIGURES = [{"name": "Orion", "abbr": "Ori", "segments": [[0.0, 0.0, 10.0, 10.0]]}]


@pytest.fixture(autouse=True)
def stub_solve(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(solver, "solve_tiered", lambda *a, **k: {
        "success": True, "wcs_path": "/fake.wcs", "total_seconds": 1.0,
        "attempts": [{"fov_bounds": [30, 90], "seconds": 1.0, "success": True}],
    })
    monkeypatch.setattr(solver, "annotate", lambda *a, **k: list(STARS))
    monkeypatch.setattr(constellations, "annotate", lambda *a: list(FIGURES))
    # Enough stars that the pre-solve gate stays open unless a test says so.
    monkeypatch.setattr(verify, "count_stars", lambda *a: 50)
    # No narration by default: tests never depend on an API key in the env.
    monkeypatch.setattr(narrate, "annotate", lambda *a, **k: None)
    # Likewise no Space-Track credentials, and never a network call.
    monkeypatch.setattr(satellites, "annotate",
                        lambda *a, **k: {"skipped": "no_credentials"})


def test_bodies_merge_ahead_of_stars(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: (list(BODIES), {"time_source": "exif_offset"}))
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["labels"] == BODIES + STARS
    assert result["ephemeris"]["time_source"] == "exif_offset"
    assert result["constellations"] == FIGURES


def test_verification_meta_survives_missing_image(monkeypatch):
    # JOB's image path doesn't exist: verification must degrade gracefully
    # and leave the labels untouched rather than sinking the solve.
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["verification"]["verified"] is False
    assert result["labels"] == STARS


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


def test_narration_attaches_when_available(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    narration = {"caption": "Sirius blazing in Orion's wake",
                 "text": "Your photo caught Sirius.", "model": "test"}
    monkeypatch.setattr(narrate, "annotate", lambda *a, **k: narration)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["narration"] == narration


def test_satellite_crossings_attach_to_the_result(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    sats = {"crossings": [{"name": "Iss (Zarya)", "norad_id": "25544",
                           "points": [[1.0, 2.0], [3.0, 4.0]],
                           "t_enter_s": 0.0, "t_exit_s": 16.0}],
            "objects_checked": 4200, "exposure_seconds": 16.0, "source": "gp"}
    monkeypatch.setattr(satellites, "annotate", lambda *a, **k: sats)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["satellites"] == sats


def test_satellite_crash_does_not_fail_the_job(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    def boom(*a, **k):
        raise RuntimeError("space-track exploded")
    monkeypatch.setattr(satellites, "annotate", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["satellites"] == {"skipped": "satellite lookup failed"}
    assert result["labels"] == STARS


def test_narration_crash_does_not_fail_the_job(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    def boom(*a, **k):
        raise RuntimeError("narration exploded")
    monkeypatch.setattr(narrate, "annotate", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert "narration" not in result


def test_no_stars_gate_fails_fast_without_solving(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 3)
    def boom(*a, **k):
        raise AssertionError("solver must not run on a zero-star image")
    monkeypatch.setattr(solver, "solve_tiered", boom)
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert result["failure"] == {"reason": "no_stars", "stars_detected": 3,
                                 "can_deepen": True,
                                 "guess_unavailable": "no_timestamp"}
    assert "star-like sources" in error


def test_failed_solve_carries_the_fallback_guess(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 3)
    guess = {"candidates": [{"name": "Venus"}], "sun_alt_deg": -8}
    monkeypatch.setattr(ephemeris, "fallback_guess", lambda e: guess)
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert result["failure"]["guess"] == guess


def test_fallback_guess_crash_does_not_mask_the_failure(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 3)
    def boom(e):
        raise RuntimeError("guess exploded")
    monkeypatch.setattr(ephemeris, "fallback_guess", boom)
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert result["failure"] == {"reason": "no_stars", "stars_detected": 3,
                                 "can_deepen": True,
                                 "guess_unavailable": "no_timestamp"}


def test_quick_mode_runs_only_the_first_tier(monkeypatch):
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None):
        seen["tiers"] = tiers
        return {"success": False, "total_seconds": 1.0, "log_tail": "",
                "attempts": [{"fov_bounds": [30.0, 90.0], "seconds": 1.0,
                              "success": False}]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    status, result, error = worker.process(JOB)
    assert seen["tiers"] == [solver.FALLBACK_TIERS[0]]  # no EXIF focal in JOB
    assert status == "failed"
    assert result["failure"] == {"reason": "no_match", "can_deepen": True,
                                 "guess_unavailable": "no_timestamp"}


def test_deep_mode_skips_tiers_the_quick_pass_tried(monkeypatch):
    monkeypatch.setattr(verify, "count_stars",
                        lambda *a: pytest.fail("no pre-check in deep mode"))
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None):
        seen["tiers"] = tiers
        return {"success": False, "total_seconds": 2.0, "log_tail": "",
                "attempts": [{"fov_bounds": [8.0, 35.0], "seconds": 2.0,
                              "success": False}]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    prior = {"attempts": [{"fov_bounds": [30.0, 90.0], "seconds": 60.0,
                           "success": False}], "total_seconds": 60.0}
    job = dict(JOB, mode="deep", result_json=json.dumps(prior))
    status, result, error = worker.process(job)
    # everything the quick pass didn't try, telephoto tier included
    assert seen["tiers"] == solver.FALLBACK_TIERS[1:]
    assert status == "failed"
    # quick attempts stay visible, times accumulate, and it's the end of the road
    assert [a["fov_bounds"] for a in result["attempts"]] == [[30.0, 90.0], [8.0, 35.0]]
    assert result["total_seconds"] == 62.0
    assert result["failure"] == {"reason": "no_match", "can_deepen": False,
                                 "guess_unavailable": "no_timestamp"}
