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


def test_explicit_tiers_override_the_plan(monkeypatch, tmp_path):
    tried = []
    monkeypatch.setattr(solver, "solve", _stub_solve(tried))
    info = {"focal_35mm": 27.0, "fov_bounds": (47.17, 94.33)}
    result = solver.solve_tiered("x.jpg", str(tmp_path), info,
                                 tiers=[(8.0, 35.0)])
    assert tried == [(8.0, 35.0)]
    assert len(result["attempts"]) == 1


def test_empty_tiers_returns_clean_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(solver, "solve", _stub_solve([]))
    result = solver.solve_tiered("x.jpg", str(tmp_path), {}, tiers=[])
    assert result["success"] is False
    assert result["attempts"] == []


def test_tier_plan_matches_solve_order():
    info = {"focal_35mm": 27.0, "fov_bounds": (47.17, 94.33)}
    assert solver.tier_plan(info) == [(47.17, 94.33)] + solver.FALLBACK_TIERS
    assert solver.tier_plan({"focal_35mm": None}) == solver.FALLBACK_TIERS


def test_ultrawide_exif_tier_skipped():
    # f35=11mm -> ~117 deg estimated field: beyond index coverage (#46),
    # so the plan starts straight at the fallbacks.
    info = {"focal_35mm": 11.0, "fov_bounds": (82.0, 164.0)}
    assert solver.tier_plan(info) == solver.FALLBACK_TIERS


def test_moderately_wide_exif_tier_kept():
    # f35=16mm -> ~96.7 deg: wide but within coverage, EXIF tier stays
    # first. Bounds follow read_exif's bracketing (fov * 0.7, fov * 1.4).
    info = {"focal_35mm": 16.0, "fov_bounds": (67.7, 135.4)}
    assert solver.tier_plan(info)[0] == (67.7, 135.4)


def test_near_duplicate_exif_tier_deduped(monkeypatch, tmp_path):
    tried = []
    monkeypatch.setattr(solver, "solve", _stub_solve(tried))
    # EXIF bounds within 2 degrees of the (30, 90) fallback on both ends
    # should suppress the duplicate fallback attempt.
    info = {"focal_35mm": 50.0, "fov_bounds": (31.0, 89.0)}
    solver.solve_tiered("x.jpg", str(tmp_path), info)
    # the EXIF tier stands in for the first fallback; the rest still run
    assert tried == [(31.0, 89.0)] + solver.FALLBACK_TIERS[1:]


def test_cfg_lists_only_fits_indexes(monkeypatch, tmp_path):
    index_dir = tmp_path / "indexes"
    index_dir.mkdir()
    (index_dir / "index-4110.fits").touch()
    (index_dir / "index-4108.fits").touch()
    (index_dir / ".gitkeep").touch()
    monkeypatch.setattr(solver, "INDEX_DIR", str(index_dir))

    cfg = solver._write_cfg(str(tmp_path))
    content = open(cfg).read()
    assert f"index {index_dir}/index-4108.fits" in content
    assert f"index {index_dir}/index-4110.fits" in content
    assert ".gitkeep" not in content
    assert "autoindex" not in content


def test_cfg_falls_back_to_autoindex_without_fits(monkeypatch, tmp_path):
    index_dir = tmp_path / "indexes"
    index_dir.mkdir()
    (index_dir / ".gitkeep").touch()
    monkeypatch.setattr(solver, "INDEX_DIR", str(index_dir))

    cfg = solver._write_cfg(str(tmp_path))
    content = open(cfg).read()
    assert "autoindex" in content
    assert f"add_path {index_dir}" in content
