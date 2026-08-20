"""The confidence floor on solve results (#71).

solve-field exiting 0 with a WCS on disk is not proof of a solve: a search cut
short by the CPU limit can still write out its best hypothesis, and that can be
a three-star triangle with two matching stars pointing somewhere confidently
wrong. These tests stub the binary out; the real-solve side of the floor lives
in test_solve_integration.py.

The floors are pinned to their calibrated defaults by the autouse fixture in
conftest, so the values below mean what they meant at calibration time no
matter how the environment is configured."""

import os

import numpy as np
import pytest
from astropy.io import fits

from app import solver


def write_match(out_dir, logodds, nmatch, ndistract=5):
    """A solve.match with the columns match_stats reads."""
    os.makedirs(out_dir, exist_ok=True)
    cols = fits.ColDefs([
        fits.Column(name="LOGODDS", format="E", array=np.array([logodds])),
        fits.Column(name="NMATCH", format="J", array=np.array([nmatch])),
        fits.Column(name="NDISTRACT", format="J", array=np.array([ndistract])),
    ])
    hdul = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.from_columns(cols)])
    hdul.writeto(os.path.join(out_dir, "solve.match"), overwrite=True)


def stub_run(out_dir, returncode=0, write_wcs=True, logodds=None, nmatch=None):
    """Stand in for the solve-field subprocess: drops the artifacts a run of
    that shape would leave behind."""
    def fake(cmd, **kwargs):
        if write_wcs:
            open(os.path.join(out_dir, "solve.wcs"), "w").close()
        if logodds is not None:
            write_match(out_dir, logodds, nmatch)

        class Proc:
            pass
        proc = Proc()
        proc.returncode = returncode
        proc.stdout = ""
        proc.stderr = ""
        return proc
    return fake


def test_match_stats_reads_the_match_table(tmp_path):
    write_match(str(tmp_path), 342.28, 192, ndistract=160)
    stats = solver.match_stats(str(tmp_path))
    assert stats == pytest.approx({"logodds": 342.28, "nmatch": 192,
                                   "ndistract": 160}, rel=1e-4)


def test_match_stats_absent_table_is_none(tmp_path):
    assert solver.match_stats(str(tmp_path)) is None


def test_match_stats_unreadable_table_is_none(tmp_path):
    (tmp_path / "solve.match").write_text("not a FITS file")
    assert solver.match_stats(str(tmp_path)) is None


def test_confident_match_is_accepted(monkeypatch, tmp_path):
    # The weakest genuine solve in the 2026-08-15 calibration set.
    monkeypatch.setattr(solver.subprocess, "run",
                        stub_run(str(tmp_path), logodds=93.1, nmatch=17))
    result = solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))
    assert result["success"] is True
    assert result["low_confidence"] is False
    assert result["wcs_path"] is not None
    assert result["match"]["nmatch"] == 17


def test_low_odds_match_is_rejected(monkeypatch, tmp_path):
    # The false WCS from #71: exit 0, WCS written, 2 matched stars.
    monkeypatch.setattr(solver.subprocess, "run",
                        stub_run(str(tmp_path), logodds=9.53, nmatch=2))
    result = solver.solve("x.jpg", str(tmp_path), (11.0, 12.5))
    assert result["success"] is False
    assert result["low_confidence"] is True
    assert result["wcs_path"] is None, "a rejected match must not be usable"
    assert result["match"]["logodds"] == pytest.approx(9.53, rel=1e-4)


def test_few_matches_rejected_despite_high_odds(monkeypatch, tmp_path):
    # Either criterion alone is disqualifying.
    monkeypatch.setattr(solver.subprocess, "run",
                        stub_run(str(tmp_path), logodds=500.0, nmatch=3))
    assert solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))["success"] is False


def test_unreadable_match_table_does_not_sink_a_solve(monkeypatch, tmp_path):
    # Best-effort by design: no readable stats means no opinion, not a veto.
    monkeypatch.setattr(solver.subprocess, "run", stub_run(str(tmp_path)))
    result = solver.solve("x.jpg", str(tmp_path), (30.0, 90.0))
    assert result["success"] is True
    assert result["match"] is None
    assert result["low_confidence"] is False


def test_rejected_tier_does_not_leak_into_the_next(monkeypatch, tmp_path):
    """The trap this gate introduces: solve_tiered reuses one directory, and
    solve-field only writes solve.wcs when it solves. A rejected match leaves
    one behind, so a later tier that solves nothing at all would otherwise be
    credited with the earlier tier's WCS."""
    out = str(tmp_path)
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            # First pass of the first tier: a low-confidence match, rejected
            # but left on disk.
            open(os.path.join(out, "solve.wcs"), "w").close()
            write_match(out, 9.53, 2)
            rc = 0
        else:
            # Every later pass: solve-field finds nothing and writes nothing.
            rc = 1

        class Proc:
            pass
        proc = Proc()
        proc.returncode = rc
        proc.stdout = ""
        proc.stderr = ""
        return proc

    monkeypatch.setattr(solver.subprocess, "run", fake)
    result = solver.solve_tiered("x.jpg", out, {"focal_35mm": None})

    # Two solve-field passes per tier: capped, then unbounded.
    assert len(calls) == 2 * len(solver.FALLBACK_TIERS), "every tier should run"
    assert result["success"] is False
    assert all(a["success"] is False for a in result["attempts"])
    assert not os.path.exists(os.path.join(out, "solve.wcs")), \
        "the rejected WCS should not survive the next attempt"


def test_quick_budget_trims_and_marks_attempts(monkeypatch, tmp_path):
    """The quick pass runs the trimmed unbounded pass only on the first tier,
    the capped pass alone on the rest, and marks every attempt thorough=False
    so deep mode re-runs the tiers instead of skipping them."""
    out = str(tmp_path)
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)

        class Proc:
            pass
        proc = Proc()
        proc.returncode = 1  # nothing ever solves
        proc.stdout = ""
        proc.stderr = ""
        return proc

    monkeypatch.setattr(solver.subprocess, "run", fake)
    result = solver.solve_tiered("x.jpg", out, {"focal_35mm": None},
                                 tiers=[(30.0, 90.0), (8.0, 35.0)], quick=True)

    def flag(cmd, name):
        return cmd[cmd.index(name) + 1] if name in cmd else None

    # Tier 1: capped pass, then the trimmed unbounded pass.
    assert flag(calls[0], "--objs") == str(solver.SOURCE_DEPTH)
    assert flag(calls[0], "--cpulimit") == str(solver.CAPPED_PASS_CPULIMIT)
    assert flag(calls[1], "--objs") is None
    assert flag(calls[1], "--cpulimit") == str(solver.QUICK_UNBOUNDED_CPULIMIT)
    # Tier 2: capped pass only.
    assert flag(calls[2], "--objs") == str(solver.SOURCE_DEPTH)
    assert len(calls) == 3
    assert all(a["thorough"] is False for a in result["attempts"])

    # The full-budget path is unchanged: two passes per tier, full cpulimit.
    calls.clear()
    result = solver.solve_tiered("x.jpg", out, {"focal_35mm": None},
                                 tiers=[(30.0, 90.0), (8.0, 35.0)])
    assert [flag(c, "--cpulimit") for c in calls] == [
        str(solver.CAPPED_PASS_CPULIMIT), str(solver.CPU_LIMIT)] * 2
    assert all(a["thorough"] is True for a in result["attempts"])
