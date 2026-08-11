"""Catalog projection through a hand-built WCS — no solver involved.
Needs catalogs/hyg.csv (mounted at /catalogs in the containers)."""

import pytest
from astropy.io import fits

from app import solver
from tests import synth

WIDTH, HEIGHT = 1000, 750


@pytest.fixture()
def orion_wcs_file(tmp_path):
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
