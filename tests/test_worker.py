"""Worker result assembly: star + body labels merge, ephemeris failures
never sink a successful solve, quick/deep gating. Everything external is
stubbed."""

import json

import pytest

from app import (beyond, constellations, db, ephemeris, locate, lore, narrate,
                 night, satellites, solver, streaks, verify, worker)

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
    monkeypatch.setattr(narrate, "annotate_failure", lambda *a, **k: None)
    # Likewise no Space-Track credentials, and never a network call.
    monkeypatch.setattr(satellites, "annotate",
                        lambda *a, **k: {"skipped": "no_credentials"})
    # The fake WCS path above would make the off-frame layer raise (and
    # log) on every test; stub it like the rest.
    monkeypatch.setattr(beyond, "annotate", lambda *a, **k: [])
    # Same for the deep catalog projection and the night context (#121,
    # #122): both read the WCS file, and neither is what these tests test.
    monkeypatch.setattr(solver, "project_deep", lambda *a, **k: None)
    monkeypatch.setattr(solver, "pointing", lambda *a, **k: None)
    monkeypatch.setattr(night, "annotate", lambda *a, **k: None)
    monkeypatch.setattr(locate, "annotate", lambda *a, **k: None)
    monkeypatch.setattr(lore, "annotate", lambda *a, **k: [])
    # Streak detection reads the image and the WCS, neither of which exists.
    monkeypatch.setattr(streaks, "annotate", lambda *a, **k: {"streaks": []})


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


def test_streaks_attach_with_the_satellite_layer_in_hand(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    sats = {"crossings": [], "objects_checked": 1, "exposure_seconds": 10.0,
            "source": "gp"}
    monkeypatch.setattr(satellites, "annotate", lambda *a, **k: sats)
    seen = {}

    def find(image_path, wcs_path, width, height, exif_info, sats_arg=None):
        seen.update(image=image_path, wcs=wcs_path, sats=sats_arg)
        return {"streaks": [{"start": [1.0, 1.0], "end": [90.0, 90.0],
                             "kind": "meteor", "confidence": "medium"}]}
    monkeypatch.setattr(streaks, "annotate", find)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["streaks"]["streaks"][0]["kind"] == "meteor"
    # The detector is handed the predicted crossings so it can name one.
    assert seen == {"image": "/photos/x.jpg", "wcs": "/fake.wcs", "sats": sats}


def test_streak_crash_does_not_fail_the_job(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    def boom(*a, **k):
        raise RuntimeError("hough overflowed")
    monkeypatch.setattr(streaks, "annotate", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["streaks"] == {"streaks": [], "error": "streak detection failed"}
    assert result["labels"] == STARS


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


def _no_stars_job(**exif_extra):
    return {"id": "abc123", "image_path": "/photos/x.jpg", "mode": "quick",
            "exif_json": json.dumps({"width": 100, "height": 100,
                                     **exif_extra})}


def test_daylight_advice_from_the_sun_altitude(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    monkeypatch.setattr(ephemeris, "fallback_guess",
                        lambda e: {"sun_alt_deg": 35, "candidates": []})
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert result["failure"]["advice"] == "daylight"


def test_twilight_advice_outranks_the_exposure(monkeypatch):
    # Sun at -8: still nautical twilight. A short exposure is true but not
    # the problem — night mode can't beat a bright sky.
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    monkeypatch.setattr(ephemeris, "fallback_guess",
                        lambda e: {"sun_alt_deg": -8, "candidates": []})
    status, result, error = worker.process(_no_stars_job(exposure_seconds=0.05))
    assert result["failure"]["advice"] == "twilight"


def test_snapshot_exposure_gets_night_mode_advice(monkeypatch):
    # The prod case: dark sky, 0.098s handheld snap, zero stars.
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    monkeypatch.setattr(ephemeris, "fallback_guess",
                        lambda e: {"sun_alt_deg": -30, "candidates": []})
    status, result, error = worker.process(_no_stars_job(exposure_seconds=0.098))
    assert result["failure"]["advice"] == "short_exposure"


def test_long_empty_exposure_blames_the_sky(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    monkeypatch.setattr(ephemeris, "fallback_guess",
                        lambda e: {"sun_alt_deg": -30, "candidates": []})
    status, result, error = worker.process(_no_stars_job(exposure_seconds=16.0))
    assert result["failure"]["advice"] == "dark_but_empty"


def test_failure_narration_attaches(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    note = {"text": "That appears to be a sandwich.", "model": "test"}
    monkeypatch.setattr(narrate, "annotate_failure", lambda *a, **k: note)
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert result["narration"] == note


def test_failure_narration_crash_does_not_mask_the_failure(monkeypatch):
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    def boom(*a, **k):
        raise RuntimeError("vision exploded")
    monkeypatch.setattr(narrate, "annotate_failure", boom)
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert "narration" not in result


def test_solved_narration_gets_the_image_path(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    seen = {}
    def record(result, image_path=None, **kw):
        seen["image_path"] = image_path
        seen["frame"] = (kw.get("width"), kw.get("height"))
        return None
    monkeypatch.setattr(narrate, "annotate", record)
    status, result, error = worker.process(JOB)
    assert status == "done"
    assert seen["image_path"] == JOB["image_path"]
    assert seen["frame"] == (100, 100)  # the upright frame, for placing labels


def test_no_advice_without_evidence(monkeypatch):
    # No guess (so no sun altitude) and no exposure: wrong advice is worse
    # than no advice, so the key stays absent.
    monkeypatch.setattr(verify, "count_stars", lambda *a: 0)
    monkeypatch.setattr(ephemeris, "fallback_guess", lambda e: None)
    status, result, error = worker.process(JOB)
    assert status == "failed"
    assert "advice" not in result["failure"]


def test_quick_mode_without_exif_runs_every_fallback_tier(monkeypatch):
    """No focal length means no hint to size the pass to, so the quick pass
    runs the whole fallback plan (#160). At one tier it stopped at (30, 90)
    and a screenshot, a re-encode or an astro camera's output — the photos
    the telephoto bracket was written for — got a failure page without the
    narrower brackets ever running."""
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None, quick=False):
        seen["tiers"] = tiers
        seen["quick"] = quick
        return {"success": False, "total_seconds": 3.0, "log_tail": "",
                "attempts": [{"fov_bounds": list(t), "seconds": 1.0,
                              "success": False, "thorough": False}
                             for t in tiers]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    status, result, error = worker.process(JOB)  # no EXIF focal in JOB
    assert seen["tiers"] == solver.FALLBACK_TIERS
    # The extra tiers are the capped pass alone, not a second full budget.
    assert seen["quick"] is True
    assert status == "failed"
    # Trimmed attempts retire nothing, so "try harder" still has somewhere
    # to go — every tier gets its full budget in deep mode.
    assert result["failure"] == {"reason": "no_match", "can_deepen": True,
                                 "guess_unavailable": "no_timestamp"}


def test_an_exif_hint_still_bounds_the_quick_pass(monkeypatch):
    """The no-EXIF widening must not leak into the hinted path: with a focal
    length the quick pass stays on the EXIF brackets and leaves the three
    fallbacks to deep mode, or every hinted upload pays for tiers its own
    scale estimate already ruled out."""
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None, quick=False):
        seen["tiers"] = tiers
        return {"success": False, "total_seconds": 1.0, "log_tail": "",
                "attempts": [{"fov_bounds": [47.17, 94.33], "seconds": 1.0,
                              "success": False, "thorough": False}]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    job = dict(JOB)
    job["exif_json"] = json.dumps({"width": 100, "height": 100,
                                   "focal_35mm": 27.0,
                                   "fov_bounds": [47.17, 94.33]})
    worker.process(job)
    assert seen["tiers"] == [(47.17, 94.33)]
    assert solver.FALLBACK_TIERS[0] not in seen["tiers"]


def test_quick_mode_runs_every_exif_tier(monkeypatch):
    """With a focal length, quick covers both split brackets: a hidden-crop
    phone shot (#57) solves in the extension tier without a "try harder"
    click, at the cost of one extra tier on photos that fail outright."""
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None, quick=False):
        seen["tiers"] = tiers
        return {"success": False, "total_seconds": 2.0, "log_tail": "",
                "attempts": [{"fov_bounds": [47.2, 80.9], "seconds": 1.0,
                              "success": False},
                             {"fov_bounds": [23.6, 47.2], "seconds": 1.0,
                              "success": False}]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    job = dict(JOB)
    job["exif_json"] = json.dumps(
        {"width": 100, "height": 100, "focal_35mm": 27.0,
         "fov_bounds": [23.6, 80.9],
         "fov_tiers": [[47.2, 80.9], [23.6, 47.2]]})
    status, result, error = worker.process(job)
    assert seen["tiers"] == [(47.2, 80.9), (23.6, 47.2)]
    assert status == "failed"
    assert result["failure"]["can_deepen"] is True  # fallbacks remain


def test_deep_mode_skips_tiers_the_quick_pass_tried(monkeypatch):
    monkeypatch.setattr(verify, "count_stars",
                        lambda *a: pytest.fail("no pre-check in deep mode"))
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None, quick=False):
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


def test_deep_mode_after_the_star_gate_runs_one_tier_not_four(monkeypatch):
    """The gate returns before the solver, so `attempts` is empty and the
    whole plan looks untried. Running it costs four cpulimits — measured at
    20 minutes of wall clock on the deploy — to reach the conclusion the
    gate already reached. One tier honours the override (#90)."""
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None, quick=False):
        seen["tiers"] = tiers
        return {"success": False, "total_seconds": 60.0, "log_tail": "",
                "attempts": [{"fov_bounds": [30.0, 90.0], "seconds": 60.0,
                              "success": False}]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    prior = {"attempts": [], "total_seconds": 0.0,
             "failure": {"reason": "no_stars", "stars_detected": 4,
                         "can_deepen": True}}
    job = dict(JOB, mode="deep", result_json=json.dumps(prior))
    status, result, error = worker.process(job)
    assert seen["tiers"] == [solver.FALLBACK_TIERS[0]]
    assert status == "failed"
    assert result["failure"]["can_deepen"] is False


def test_deep_mode_after_a_real_solve_attempt_is_not_capped(monkeypatch):
    """Only the gate path is capped. A quick pass that actually ran the
    solver and found no match still gets every remaining scale tier — there
    were stars, so the field size is the open question."""
    seen = {}
    def record(image_path, out_dir, exif_info, tiers=None, quick=False):
        seen["tiers"] = tiers
        return {"success": False, "total_seconds": 2.0, "log_tail": "",
                "attempts": [], "fov_bounds": [8.0, 35.0]}
    monkeypatch.setattr(solver, "solve_tiered", record)
    prior = {"attempts": [{"fov_bounds": [30.0, 90.0], "seconds": 60.0,
                           "success": False}], "total_seconds": 60.0,
             "failure": {"reason": "no_match", "can_deepen": True}}
    job = dict(JOB, mode="deep", result_json=json.dumps(prior))
    worker.process(job)
    assert seen["tiers"] == solver.FALLBACK_TIERS[1:]


def test_periodic_tasks_are_due_on_the_first_pass():
    """time.monotonic() counts from boot, and a Fly machine that auto-stops
    is a fresh boot on every wake (measured: 85.9s on a machine that had
    been serving for minutes). Comparing against an initial 0.0 asks for a
    full interval of *continuous uptime* first — 15 minutes for the sweep,
    10 for notifications — so on a site quiet enough to let the machine
    stop, neither would ever run. Local docker hides it: containers share
    the host's monotonic clock, which is days large."""
    assert worker.due(None, 900) is True
    # Once it has run, the interval applies normally.
    assert worker.due(100.0, 900, now=150.0) is False
    assert worker.due(100.0, 900, now=1500.0) is True
    # A freshly-booted machine is exactly the case that used to fail: a
    # small monotonic reading is not evidence that the task just ran.
    assert worker.due(None, 900, now=85.9) is True


def test_beyond_pointers_ride_along_and_never_sink_the_job(monkeypatch):
    monkeypatch.setattr(ephemeris, "annotate_bodies",
                        lambda *a: ([], {"time_source": None}))
    pointer = {"name": "Saturn", "kind": "planet", "mag": 0.7,
               "edge_x": 100.0, "edge_y": 50.0, "ux": 1.0, "uy": 0.0,
               "deg": 8.0, "side": "right"}
    monkeypatch.setattr(beyond, "annotate", lambda *a, **k: [pointer])
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["beyond"] == [pointer]

    def boom(*a, **k):
        raise RuntimeError("wcs exploded")
    monkeypatch.setattr(beyond, "annotate", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["beyond"] == []


def test_night_context_is_stored_and_handed_to_the_narrator(monkeypatch):
    lines = ["The Moon was new, so it added no light to the sky."]
    seen = {}
    monkeypatch.setattr(
        night, "annotate",
        lambda exif, wcs, labels, pointers, verification: {"lines": list(lines)})

    def narrate_stub(result, image_path=None, client=None, **kw):
        seen["night"] = result.get("night")  # built before the narration runs
        return None
    monkeypatch.setattr(narrate, "annotate", narrate_stub)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["night"] == {"lines": lines}
    assert seen["night"] == {"lines": lines}


def test_night_context_failure_never_sinks_a_solve(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ephemeris exploded")
    monkeypatch.setattr(night, "annotate", boom)
    status, result, error = worker.process(JOB)
    assert status == "done" and error is None
    assert result["night"] is None
    assert result["labels"]  # the rest of the result is untouched


def test_deep_catalog_projection_failure_still_verifies(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no such wcs")
    monkeypatch.setattr(solver, "project_deep", boom)
    calls = []

    def apply_stub(image_path, labels, figures, deep=None):
        calls.append(deep)
        return labels, figures, {"verified": True}
    monkeypatch.setattr(verify, "apply", apply_stub)
    status, result, _ = worker.process(JOB)
    assert status == "done"
    assert calls == [None]  # verification ran, just without a depth estimate


def test_place_is_stored_and_its_failure_is_survived(monkeypatch):
    line = "The phone recorded its tilt, so sky geometry puts this near 36°N, 74°E: northern Pakistan."
    monkeypatch.setattr(locate, "annotate",
                        lambda wcs, w, h, exif: {"source": "tilt", "line": line})
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["place"]["line"] == line

    def boom(*a, **k):
        raise RuntimeError("map missing")
    monkeypatch.setattr(locate, "annotate", boom)
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["place"] is None


def test_pointing_summary_rides_on_the_result(monkeypatch):
    monkeypatch.setattr(solver, "pointing",
                        lambda wcs, w, h: {"ra": 84.0, "dec": 0.0, "arcsec_per_px": 144.0, "fov_deg": [4.0, 4.0]})
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["pointing"]["ra"] == 84.0

    def boom(*a, **k):
        raise RuntimeError("no wcs")
    monkeypatch.setattr(solver, "pointing", boom)
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["pointing"] is None


def test_lore_rides_on_the_result_and_its_failure_is_survived(monkeypatch):
    entry = {"abbr": "Ori", "name": "Orion", "line": lore.LORE["Ori"]}
    monkeypatch.setattr(lore, "annotate", lambda figures, labels: [entry])
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["lore"] == [entry]

    def boom(*a, **k):
        raise RuntimeError("no catalog")
    monkeypatch.setattr(lore, "annotate", boom)
    status, result, _ = worker.process(JOB)
    assert status == "done" and result["lore"] == []
