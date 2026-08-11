"""End-to-end solver tests against synthetic images with known ground truth.
Marked `solver`: needs the solve-field binary and the wide-field indexes
(run inside the worker container: pytest -m solver)."""

import math

import pytest
from astropy.io import fits
from astropy.wcs import WCS

from app import exif, solver
from tests import synth

pytestmark = pytest.mark.solver

TRUTH = {"ra": 95.0, "dec": -10.0, "fov_deg": 50.0}


@pytest.fixture(scope="module")
def starfield(tmp_path_factory):
    path = tmp_path_factory.mktemp("synth") / "starfield.jpg"
    synth.render_starfield(str(path), **TRUTH)
    return path


def _angular_sep(ra1, dec1, ra2, dec2):
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(dec1) * math.sin(dec2)
         + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def test_synthetic_field_solves_with_exif_hint(starfield, tmp_path):
    info = exif.read_exif(starfield)
    # the baked-in 39mm-equivalent focal length should hint the first tier
    assert info["focal_35mm"] == 39.0
    assert info["fov_bounds"][0] < TRUTH["fov_deg"] < info["fov_bounds"][1]

    result = solver.solve_tiered(str(starfield), str(tmp_path), info)
    assert result["success"], result["log_tail"]
    assert result["attempts"][0]["success"], "EXIF-hinted tier should solve directly"

    with fits.open(result["wcs_path"]) as hdul:
        wcs = WCS(hdul[0].header)
    ra, dec = wcs.all_pix2world(info["width"] / 2, info["height"] / 2, 0)
    assert _angular_sep(float(ra), float(dec), TRUTH["ra"], TRUTH["dec"]) < 1.5

    labels = solver.annotate(result["wcs_path"], info["width"], info["height"])
    assert labels, "solved field should contain named bright stars"
    assert any(l["name"] == "Sirius" for l in labels)


@pytest.mark.parametrize("render", [
    synth.render_noise, synth.render_black, synth.render_gradient,
], ids=["noise", "black", "gradient"])
def test_unsolvable_images_fail_gracefully(render, tmp_path, monkeypatch):
    monkeypatch.setattr(solver, "CPU_LIMIT", 10)
    path = tmp_path / "bad.jpg"
    render(str(path))

    info = exif.read_exif(path)
    result = solver.solve_tiered(str(path), str(tmp_path / "out"), info)

    assert result["success"] is False
    assert result["wcs_path"] is None
    # every fallback tier was attempted, none crashed
    assert len(result["attempts"]) == len(solver.FALLBACK_TIERS)
    assert all(a["success"] is False for a in result["attempts"])
