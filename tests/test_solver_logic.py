"""Tier-selection logic in solve_tiered, with solve() stubbed out."""

from app import solver


def _stub_solve(record, succeed_on=None):
    def fake(image_path, out_dir, fov_bounds):
        record.append(fov_bounds)
        success = succeed_on is not None and len(record) == succeed_on
        return {
            "success": success,
            "seconds": 1.0,
            "wcs_path": "/fake.wcs" if success else None,
            "fov_bounds": [round(fov_bounds[0], 1), round(fov_bounds[1], 1)],
            "log_tail": "",
        }
    return fake


def test_no_exif_uses_fallback_tiers(monkeypatch, tmp_path):
    tried = []
    monkeypatch.setattr(solver, "solve", _stub_solve(tried))
    result = solver.solve_tiered("x.jpg", str(tmp_path), {"focal_35mm": None})
    assert tried == solver.FALLBACK_TIERS
    assert result["success"] is False
    assert len(result["attempts"]) == len(solver.FALLBACK_TIERS)
    assert result["total_seconds"] == len(solver.FALLBACK_TIERS)


def test_exif_tier_tried_first_then_fallbacks(monkeypatch, tmp_path):
    tried = []
    monkeypatch.setattr(solver, "solve", _stub_solve(tried))
    info = {"focal_35mm": 27.0, "fov_bounds": (47.17, 94.33)}
    result = solver.solve_tiered("x.jpg", str(tmp_path), info)
    assert tried[0] == (47.17, 94.33)
    assert tried[1:] == solver.FALLBACK_TIERS
    assert result["success"] is False


def test_success_stops_tier_iteration(monkeypatch, tmp_path):
    tried = []
    monkeypatch.setattr(solver, "solve", _stub_solve(tried, succeed_on=1))
    info = {"focal_35mm": 27.0, "fov_bounds": (47.17, 94.33)}
    result = solver.solve_tiered("x.jpg", str(tmp_path), info)
    assert len(tried) == 1
    assert result["success"] is True
    assert result["attempts"][0]["success"] is True


def test_near_duplicate_exif_tier_deduped(monkeypatch, tmp_path):
    tried = []
    monkeypatch.setattr(solver, "solve", _stub_solve(tried))
    # EXIF bounds within 2 degrees of the (30, 90) fallback on both ends
    # should suppress the duplicate fallback attempt.
    info = {"focal_35mm": 50.0, "fov_bounds": (31.0, 89.0)}
    solver.solve_tiered("x.jpg", str(tmp_path), info)
    assert tried == [(31.0, 89.0), (8.0, 35.0)]
