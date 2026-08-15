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

    # A real solve should clear the #71 confidence floor with room to spare,
    # not scrape past it. Calibration put genuine solves at log-odds 93+ with
    # 17+ matched stars against floors of 25 and 8. Compared against the
    # calibrated defaults rather than the live (env-overridable) floors, so a
    # deployment-specific setting cannot make this assertion mean something
    # else.
    stats = result["attempts"][0].get("match")
    assert stats, "a successful solve records its match statistics"
    assert stats["logodds"] > 3 * solver.DEFAULT_MIN_LOGODDS, stats
    assert stats["nmatch"] > 2 * solver.DEFAULT_MIN_MATCHES, stats


# Phone telephoto: a 10x periscope (~240mm equivalent) is ~8.6 deg wide, a
# 5x (~120mm) ~17. Nothing was built for these — they solve because the
# shipped indexes reach narrower than the fallback tiers ever ask. These
# tests exist so an index-fetch change can't silently drop that coverage.
@pytest.mark.parametrize("fov_deg,f35mm", [(17.1, 120), (8.6, 240)],
                         ids=["5x-telephoto", "10x-periscope"])
def test_phone_telephoto_solves_from_its_exif_hint(fov_deg, f35mm, tmp_path):
    path = tmp_path / "tele.jpg"
    synth.render_starfield(str(path), ra=95.0, dec=-10.0, fov_deg=fov_deg,
                           width=1600, height=1200, max_mag=9.0, f35mm=f35mm)
    info = exif.read_exif(path)
    assert info["fov_bounds"][0] < fov_deg < info["fov_bounds"][1]

    result = solver.solve_tiered(str(path), str(tmp_path / "out"), info)
    assert result["success"], result["log_tail"]
    assert result["attempts"][0]["success"], "the EXIF tier should solve directly"

    with fits.open(result["wcs_path"]) as hdul:
        wcs = WCS(hdul[0].header)
    ra, dec = wcs.all_pix2world(info["width"] / 2, info["height"] / 2, 0)
    assert _angular_sep(float(ra), float(dec), 95.0, -10.0) < 1.0


def test_narrow_field_solves_from_the_fallback_tier_without_exif(tmp_path):
    # A telephoto shot stripped of EXIF (screenshot, re-encode): no focal
    # length to hint with, so only the fallback chain can catch it.
    path = tmp_path / "noexif.jpg"
    synth.render_starfield(str(path), ra=95.0, dec=-10.0, fov_deg=4.0,
                           width=1600, height=1200, max_mag=9.0, f35mm=None)
    info = exif.read_exif(path)
    assert info["focal_35mm"] is None

    tiers = solver.tier_plan(info)
    assert tiers == solver.FALLBACK_TIERS, "no EXIF: the plan is the fallbacks"
    result = solver.solve_tiered(str(path), str(tmp_path / "out"), info,
                                 tiers=[solver.FALLBACK_TIERS[-1]])
    assert result["success"], result["log_tail"]


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
