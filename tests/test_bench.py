"""The bench harness has to run what the site runs.

`run_one` is the evidence source for every tier and threshold argument
(#99), so a quick bench pass that does not size its tiers the way the
worker does is measuring a mode nobody ships — which is exactly how the
tier-coverage bug in #160 stayed invisible: the harness passed
`tiers=None` and ran the full plan on the quick budget, so the first
bracket never looked like a stopping point.
"""
import pytest

from app import bench, solver


def _capture(monkeypatch, result=None):
    seen = {}

    def fake_solve_tiered(image_path, out_dir, exif_info, tiers=None, quick=False):
        seen["tiers"] = tiers
        seen["quick"] = quick
        return result or {"success": False, "total_seconds": 1.0,
                          "attempts": [], "fov_bounds": [0.0, 0.0]}

    monkeypatch.setattr(solver, "solve_tiered", fake_solve_tiered)
    monkeypatch.setattr(bench.verify, "count_stars", lambda *a: 42)
    return seen


def test_bench_quick_pass_uses_the_workers_tiers(monkeypatch, tmp_path):
    """Without EXIF that is the whole fallback plan — the same list
    worker.process would run, not `tiers=None`, which would let the bench
    solve on a bracket the site's quick pass never reaches."""
    monkeypatch.setattr(bench.exif, "read_exif", lambda p: {"focal_35mm": None})
    seen = _capture(monkeypatch)
    bench.run_one(str(tmp_path / "x.jpg"), quick=True)
    assert seen["tiers"] == solver.quick_tiers({"focal_35mm": None})
    assert seen["tiers"] == solver.FALLBACK_TIERS
    assert seen["quick"] is True


def test_bench_quick_pass_respects_an_exif_hint(monkeypatch, tmp_path):
    info = {"focal_35mm": 27.0, "fov_bounds": (23.6, 80.9),
            "fov_tiers": [[47.2, 80.9], [23.6, 47.2]]}
    monkeypatch.setattr(bench.exif, "read_exif", lambda p: info)
    seen = _capture(monkeypatch)
    bench.run_one(str(tmp_path / "x.jpg"), quick=True)
    assert seen["tiers"] == [(47.2, 80.9), (23.6, 47.2)]
    assert not set(solver.FALLBACK_TIERS) & set(seen["tiers"])


def test_bench_deep_pass_runs_the_whole_plan(monkeypatch, tmp_path):
    """Deep mode is unbounded by design: tiers=None lets solve_tiered plan
    it, which is what the worker's deepen path ends up running."""
    info = {"focal_35mm": 27.0, "fov_bounds": (47.17, 94.33)}
    monkeypatch.setattr(bench.exif, "read_exif", lambda p: info)
    seen = _capture(monkeypatch)
    bench.run_one(str(tmp_path / "x.jpg"), quick=False)
    assert seen["tiers"] is None
    assert seen["quick"] is False
