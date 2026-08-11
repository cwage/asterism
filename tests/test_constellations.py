"""Constellation line set: fab parsing + HIP resolution against HYG, and
WCS projection. An Orion-centered frame gives a known answer — Betelgeuse
must appear as a segment endpoint exactly where the WCS puts it."""

import os

import pytest
from astropy.io import fits

from app import constellations
from tests import synth

needs_data = pytest.mark.skipif(
    not (os.path.exists(os.path.join(constellations.CATALOG_DIR,
                                     constellations.LINES_FILE))
         and os.path.exists(os.path.join(constellations.CATALOG_DIR, "hyg.csv"))),
    reason="constellations.fab / hyg.csv not fetched (scripts/fetch-catalog.sh)",
)

BETELGEUSE = (88.7929, 7.407063)  # HIP 27989, per HYG


@needs_data
def test_load_lines_resolves_the_sky():
    figures = {f["abbr"]: f for f in constellations.load_lines()}
    # All 88 IAU constellations are in the Stellarium set; nearly all their
    # stars resolve through HYG.
    assert len(figures) >= 85
    assert figures["Ori"]["name"] == "Orion"
    assert len(figures["Ori"]["segments"]) >= 10
    for fig in figures.values():
        for (ra1, dec1), (ra2, dec2) in fig["segments"]:
            assert 0.0 <= ra1 < 360.0 and 0.0 <= ra2 < 360.0
            assert -90.0 <= dec1 <= 90.0 and -90.0 <= dec2 <= 90.0


@needs_data
def test_orion_frame_draws_orion_where_the_wcs_says(tmp_path):
    width, height = 1200, 900
    wcs = synth.make_wcs(ra=83.0, dec=2.0, fov_deg=40.0,
                         width=width, height=height)
    wcs_path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(wcs_path)

    figures = {f["name"]: f for f in
               constellations.annotate(str(wcs_path), width, height)}
    assert "Orion" in figures
    orion = figures["Orion"]
    assert orion["abbr"] == "Ori"
    assert len(orion["segments"]) >= 5

    # Every kept segment touches the frame and stays within sane bounds.
    lim = 2.0 * max(width, height)
    for x1, y1, x2, y2 in orion["segments"]:
        assert (0 <= x1 < width and 0 <= y1 < height) or \
               (0 <= x2 < width and 0 <= y2 < height)
        assert max(abs(x1), abs(y1), abs(x2), abs(y2)) <= lim

    # Betelgeuse lands exactly where the ground-truth WCS projects it.
    bx, by = wcs.all_world2pix(*BETELGEUSE, 0)
    endpoints = [(x1, y1) for x1, y1, _, _ in orion["segments"]] + \
                [(x2, y2) for _, _, x2, y2 in orion["segments"]]
    assert any(abs(x - bx) < 1.0 and abs(y - by) < 1.0 for x, y in endpoints)


def test_missing_line_file_is_quiet(monkeypatch):
    monkeypatch.setattr(constellations, "CATALOG_DIR", "/nonexistent")
    monkeypatch.setattr(constellations, "_lines_cache", None)
    assert constellations.load_lines() == []
    # annotate short-circuits before ever opening the WCS.
    assert constellations.annotate("/also/nonexistent.wcs", 100, 100) == []
