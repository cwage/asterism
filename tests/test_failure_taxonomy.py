"""Telling "we searched and it isn't there" apart from "we ran out of time" (#72).

Every failure in the first round of outside uploads was a CPU-limit timeout
reported as "no solution (tried field widths: ...)" — copy that claims a search
happened when it didn't. These cover the marker detection in the solver and the
message the worker builds from it."""

import json
import os

import pytest

from app import ephemeris, solver, verify, worker

TIMEOUT_LOG = """\
Reading file "/data/jobs/x/solve.axy"...
CPU time limit reached!
Field 1 did not solve (index index-4112.fits).
Did not solve (or no WCS file was written).
"""


def stub_run(out_dir, returncode=1, output="", write_wcs=False):
    def fake(cmd, **kwargs):
        if write_wcs:
            open(os.path.join(out_dir, "solve.wcs"), "w").close()

        class Proc:
            pass
        proc = Proc()
        proc.returncode = returncode
        proc.stdout = output
        proc.stderr = ""
        return proc
    return fake


def test_cpu_limit_marker_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(solver.subprocess, "run",
                        stub_run(str(tmp_path), output=TIMEOUT_LOG))
    result = solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))
    assert result["success"] is False
    assert result["timed_out"] is True


def test_exhausted_search_is_not_a_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(solver.subprocess, "run", stub_run(
        str(tmp_path), output="Field 1 did not solve.\nDid not solve.\n"))
    result = solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))
    assert result["success"] is False
    assert result["timed_out"] is False


def test_marker_far_above_the_log_tail_is_still_seen(monkeypatch, tmp_path):
    # The marker is regularly buried under more than the 15 lines log_tail
    # keeps, which is why detection reads the whole output.
    noisy = TIMEOUT_LOG + "\n".join(f"teardown line {i}" for i in range(40))
    monkeypatch.setattr(solver.subprocess, "run",
                        stub_run(str(tmp_path), output=noisy))
    result = solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))
    assert result["timed_out"] is True
    assert solver.CPU_LIMIT_MARKER not in result["log_tail"], \
        "this test is pointless if the marker still lands in the tail"


def test_successful_solve_is_never_marked_timed_out(monkeypatch, tmp_path):
    # solve-field can print the marker for one index and then solve on another.
    monkeypatch.setattr(solver.subprocess, "run", stub_run(
        str(tmp_path), returncode=0, output=TIMEOUT_LOG, write_wcs=True))
    result = solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))
    assert result["success"] is True
    assert result["timed_out"] is False


def attempt(low, high, timed_out):
    return {"fov_bounds": [low, high], "seconds": 60.0, "success": False,
            "timed_out": timed_out}


def test_all_timeouts_say_so():
    reason, message = worker._describe_failure(
        [attempt(30, 90, True), attempt(8, 35, True)])
    assert reason == "timeout"
    assert "ran out of solve time" in message
    assert "30-90deg, 8-35deg" in message
    assert "no solution" not in message, "nothing was ruled out"


def test_exhausted_search_keeps_the_original_copy():
    reason, message = worker._describe_failure([attempt(30, 90, False)])
    assert reason == "no_match"
    assert message == "no solution (tried field widths: 30-90deg)"


def test_mixed_outcome_reports_both():
    reason, message = worker._describe_failure(
        [attempt(30, 90, True), attempt(8, 35, False)])
    assert reason == "partial_timeout"
    assert "no solution at 8-35deg" in message
    assert "ran out of solve time at 30-90deg" in message


def test_attempts_missing_the_flag_read_as_searched():
    # Rows written before #72 have no timed_out key; they must not be
    # retroactively relabelled as timeouts.
    reason, _ = worker._describe_failure(
        [{"fov_bounds": [30, 90], "seconds": 60.0, "success": False}])
    assert reason == "no_match"


def test_worker_reports_a_timeout_end_to_end(monkeypatch, tmp_path):
    from app import db

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(verify, "count_stars", lambda *a: 50)
    monkeypatch.setattr(ephemeris, "fallback_guess", lambda e: None)
    monkeypatch.setattr(solver, "solve_tiered", lambda *a, **k: {
        "success": False, "wcs_path": None, "total_seconds": 60.0,
        "attempts": [attempt(30, 90, True)],
    })
    job = {"id": "t1", "image_path": "/photos/x.jpg", "mode": "quick",
           "exif_json": json.dumps({"width": 100, "height": 100})}

    status, result, error = worker.process(job)
    assert status == "failed"
    assert result["failure"]["reason"] == "timeout"
    assert "ran out of solve time" in error
    # Two fallback tiers were never reached, so digging deeper is still worth
    # offering.
    assert result["failure"]["can_deepen"] is True
