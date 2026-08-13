"""DSO catalog filtering and WCS projection (#16).
Self-contained: uses the mini DSO fixture, not catalogs/dso.csv."""

import pytest
from astropy.io import fits

from app import dso
from tests import synth

WIDTH, HEIGHT = 1000, 750


def test_load_catalog_filters_and_names(mini_dso_catalog):
    names = {o["name"] for o in dso.load_catalog()}
    # Marquee objects get household display names even when the csv has none.
    assert "Pleiades (M45)" in names
    assert "Andromeda Galaxy (M31)" in names
    assert "Orion Nebula (M42)" in names
    assert "Beehive Cluster (M44)" in names
    # Dark nebulae, anonymous non-Messier groups, and faint objects drop out.
    assert "Coalsack" not in names
    assert "Col 135" not in names
    assert not any("M 37" in n for n in names)
    assert len(names) == 4


def test_load_catalog_converts_ra_and_sizes(mini_dso_catalog):
    m45 = next(o for o in dso.load_catalog() if "M45" in o["name"])
    assert m45["ra"] == pytest.approx(3.79 * 15.0)
    assert m45["dec"] == pytest.approx(24.117)
    # r1=110 arcmin diameter -> 55 arcmin radius, in degrees.
    assert m45["radius_deg"] == pytest.approx(110 / 60 / 2)
    mags = [o["mag"] for o in dso.load_catalog()]
    assert mags == sorted(mags)


@pytest.fixture()
def taurus_wcs_file(tmp_path, mini_dso_catalog):
    # 40 deg field centered near the Pleiades (56.9, +24.1).
    wcs = synth.make_wcs(ra=56.9, dec=24.1, fov_deg=40.0,
                         width=WIDTH, height=HEIGHT)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    return path, wcs


def test_annotate_projects_pleiades(taurus_wcs_file):
    path, wcs = taurus_wcs_file
    labels = dso.annotate(str(path), WIDTH, HEIGHT)
    assert labels, "Pleiades should project in-frame"
    m45 = next(l for l in labels if "M45" in l["name"])
    assert m45["kind"] == "dso"
    # The catalog type rides along so verification can tell resolved
    # clusters from diffuse glow (#50).
    assert m45["dso_type"] == "OC"
    assert 0 <= m45["x"] < WIDTH and 0 <= m45["y"] < HEIGHT
    # 40 deg over 1000 px -> 0.04 deg/px; 0.917 deg radius -> ~23 px.
    assert m45["radius_px"] == pytest.approx(0.917 / 0.04, rel=0.1)
    # Andromeda (ra 10.7, dec 41.3) is ~50 deg away: out of frame.
    assert not any("M31" in l["name"] for l in labels)
