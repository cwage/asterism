"""load_catalog filtering and Bayer-designation fallback.
Self-contained: uses the mini catalog fixture, not catalogs/hyg.csv."""

from app import solver


def test_proper_names_still_win(mini_catalog):
    names = {s["name"] for s in solver.load_catalog()}
    assert {"Sirius", "Betelgeuse", "Rigel", "Vega"} <= names
    # Sirius has a Bayer designation in the fixture; the proper name wins.
    assert "α CMa" not in names


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
