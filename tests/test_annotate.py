"""Catalog projection through a hand-built WCS — no solver involved.
Self-contained: uses the mini catalog fixture, not catalogs/hyg.csv."""

import pytest
from astropy.io import fits

from app import solver
from tests import synth

WIDTH, HEIGHT = 1000, 750


@pytest.fixture()
def orion_wcs_file(tmp_path, mini_catalog):
    # 40 deg field centered between Betelgeuse (88.8, +7.4) and Rigel
    # (78.6, -8.2): both should project comfortably in-frame.
    wcs = synth.make_wcs(ra=84.0, dec=0.0, fov_deg=40.0,
                         width=WIDTH, height=HEIGHT)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    return path, wcs


def test_annotate_labels_orion(orion_wcs_file):
    path, wcs = orion_wcs_file
    labels = solver.annotate(str(path), WIDTH, HEIGHT)
    names = {l["name"] for l in labels}
    assert {"Betelgeuse", "Rigel"} <= names

    for l in labels:
        assert 0 <= l["x"] < WIDTH
        assert 0 <= l["y"] < HEIGHT

    # brightest-first ordering comes from the catalog sort
    mags = [l["mag"] for l in labels]
    assert mags == sorted(mags)


def test_annotate_pixel_positions_match_wcs(orion_wcs_file):
    path, wcs = orion_wcs_file
    labels = solver.annotate(str(path), WIDTH, HEIGHT)
    betelgeuse = next(l for l in labels if l["name"] == "Betelgeuse")
    star = next(s for s in solver.load_catalog() if s["name"] == "Betelgeuse")
    x, y = wcs.all_world2pix(star["ra"], star["dec"], 0)
    assert betelgeuse["x"] == pytest.approx(float(x), abs=1.0)
    assert betelgeuse["y"] == pytest.approx(float(y), abs=1.0)


def test_annotate_respects_max_labels(orion_wcs_file):
    path, _ = orion_wcs_file
    labels = solver.annotate(str(path), WIDTH, HEIGHT, max_labels=3)
    assert len(labels) == 3


# ---- project_deep (#122): in-frame cut, RA wrap, ordering ----

def test_project_deep_crosses_the_ra_wrap_and_drops_the_far_star(tmp_path, monkeypatch):
    from tests.test_catalog import _deep_catalog
    _deep_catalog(tmp_path, monkeypatch)
    # A 20 degree field straddling RA 0: stars at 359.5 and 0.5 both land,
    # the one at RA 180 lands nowhere near the frame.
    wcs = synth.make_wcs(ra=0.0, dec=10.0, fov_deg=20.0, width=WIDTH, height=HEIGHT)
    path = tmp_path / "wrap.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    deep = solver.project_deep(str(path), WIDTH, HEIGHT, max_mag=9.0)
    assert [m for _, _, m in deep] == [3.0, 5.5, 7.9]  # brightest first
    for x, y, _ in deep:
        assert 0 <= x < WIDTH and 0 <= y < HEIGHT
    east, west = deep[0], deep[1]
    # RA grows to the left: the RA 0.5 star sits left of the RA 359.5 one
    assert east[0] < WIDTH / 2 < west[0]
    ex, ey = wcs.all_world2pix(0.5, 10.0, 0)
    assert east[0] == pytest.approx(float(ex), abs=0.01)
    assert east[1] == pytest.approx(float(ey), abs=0.01)


def test_project_deep_is_empty_when_the_field_is_elsewhere(tmp_path, monkeypatch):
    from tests.test_catalog import _deep_catalog
    _deep_catalog(tmp_path, monkeypatch)
    wcs = synth.make_wcs(ra=90.0, dec=-60.0, fov_deg=20.0, width=WIDTH, height=HEIGHT)
    path = tmp_path / "south.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    assert solver.project_deep(str(path), WIDTH, HEIGHT, max_mag=9.0) == []


def test_pointing_reads_the_centre_and_scale_off_the_wcs(orion_wcs_file):
    path, wcs = orion_wcs_file
    p = solver.pointing(str(path), WIDTH, HEIGHT)
    assert p["ra"] == pytest.approx(84.0, abs=0.05) and p["dec"] == pytest.approx(0.0, abs=0.05)
    assert p["arcsec_per_px"] == pytest.approx(40.0 / WIDTH * 3600.0, rel=0.01)
    assert p["fov_deg"] == [40.0, 30.0]
