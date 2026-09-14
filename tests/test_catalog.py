"""load_catalog filtering and Bayer-designation fallback.
Self-contained: uses the mini catalog fixture, not catalogs/hyg.csv."""

import pytest

from app import solver


def test_proper_names_still_win(mini_catalog):
    names = {s["name"] for s in solver.load_catalog()}
    assert {"Sirius", "Betelgeuse", "Rigel", "Vega"} <= names
    # Sirius has a Bayer designation in the fixture; the proper name wins.
    assert "α CMa" not in names


def test_constellation_membership_for_feed_fallback(mini_catalog):
    membership = {s["name"]: s["con"] for s in solver.load_catalog()}
    assert membership["Rigel"] == "Ori"
    assert membership["α Lup"] == "Lup"


def test_unnamed_bright_star_gets_bayer_designation(mini_catalog):
    names = {s["name"] for s in solver.load_catalog()}
    assert "α Lup" in names


def test_superscript_component_index(mini_catalog):
    names = {s["name"] for s in solver.load_catalog()}
    assert "γ² Vel" in names


def test_unnamed_secondary_component_skipped(mini_catalog):
    # Row 14 (ζ UMa, comp=2) would land on the same pixel as its primary.
    names = {s["name"] for s in solver.load_catalog()}
    assert "ζ UMa" not in names


def test_existing_filters_unchanged(mini_catalog):
    stars = solver.load_catalog()
    names = {s["name"] for s in stars}
    assert "Sol" not in names
    assert "Faintstar" not in names  # mag 5.2 > 4.5
    assert all(s["mag"] <= 4.5 for s in stars)
    # Row 10: unnamed, no Bayer designation — still dropped.
    assert len([s for s in stars if not s["name"]]) == 0


def test_bayer_name_parsing():
    assert solver._bayer_name({"bayer": "Alp", "con": "Lup"}) == "α Lup"
    assert solver._bayer_name({"bayer": "Gam-2", "con": "Vel"}) == "γ² Vel"
    # Unhyphenated index form, in case the unpinned upstream csv drifts.
    assert solver._bayer_name({"bayer": "Gam2", "con": "Vel"}) == "γ² Vel"
    assert solver._bayer_name({"bayer": "", "con": "Aur"}) is None
    assert solver._bayer_name({"bayer": "Alp", "con": ""}) is None
    assert solver._bayer_name({"bayer": "Xyz", "con": "Ori"}) is None
    assert solver._bayer_name({}) is None


# ---- the deep catalog behind the depth estimate (#122) ----

DEEP_HYG = """id,proper,ra,dec,mag,bayer,con,comp
1,Sol,0.0,0.0,-26.7,,,
2,Wrap East,0.033333,10.0,3.0,,,1
3,Wrap West,23.966667,10.0,5.5,,,1
4,,0.05,12.0,7.9,,,1
5,Too Faint,0.02,10.5,9.5,,,1
6,Far Away,12.0,10.0,2.0,,,1
7,,0.04,11.0,6.0,Zet,UMa,2
"""


def _deep_catalog(tmp_path, monkeypatch):
    (tmp_path / "hyg.csv").write_text(DEEP_HYG)
    monkeypatch.setattr(solver, "CATALOG_DIR", str(tmp_path))
    monkeypatch.setattr(solver, "_deep_cache", None)


def test_deep_catalog_keeps_the_faint_and_unnamed_but_not_sol_or_secondaries(
        tmp_path, monkeypatch):
    _deep_catalog(tmp_path, monkeypatch)
    ras, decs, mags = solver.load_deep_catalog(max_mag=9.0)
    assert sorted(mags.tolist()) == [2.0, 3.0, 5.5, 7.9]
    # RA comes back in degrees, like load_catalog
    assert min(ras) == pytest.approx(0.5, abs=1e-4)
    assert max(ras) == pytest.approx(359.5, abs=1e-4)
    # a second call is the cache, a different limit is not
    assert solver.load_deep_catalog(max_mag=9.0) is solver.load_deep_catalog(max_mag=9.0)
    assert len(solver.load_deep_catalog(max_mag=4.0)[2]) == 2
