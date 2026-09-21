"""Bright objects just outside the frame (#118): edge crossings, sky
separations, direction words, caps. Geometry is checked on a synthetic
gnomonic WCS with made-up candidates at chosen coordinates, so every
expected value can be worked by hand; one run on the real catalog checks
the whole thing is cheap and sane."""

import math
import os
import time

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import Sip, WCS

from app import beyond, ephemeris
from tests import synth

WIDTH, HEIGHT = 2000, 1500
FOV = 20.0  # degrees across the width: 0.01 deg/px on the tangent plane

# The frame's edges sit 10 (left/right) and 7.5 (top/bottom) tangent-plane
# degrees from the centre; on the sky that is atan(tan-plane) degrees.
EDGE_X_DEG = math.degrees(math.atan(math.radians(10.0)))   # 9.90
EDGE_Y_DEG = math.degrees(math.atan(math.radians(7.5)))    # 7.46
# synth.make_wcs sets CRPIX in FITS's 1-based convention, so its tangent
# point sits one pixel (0.01 deg) from the frame's geometric centre; with
# distances rounded to a tenth of a degree, expectations get that much slack.
DEG = 0.1
PX = 1.5


@pytest.fixture()
def equator_wcs_file(tmp_path):
    # Tangent point on the equator: an object at Dec 0 lands on the frame's
    # horizontal centreline exactly, so the geometry is one-dimensional.
    # RA grows to the left (cd[0][0] < 0) and Dec grows downward.
    wcs = synth.make_wcs(ra=90.0, dec=0.0, fov_deg=FOV, width=WIDTH, height=HEIGHT)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    return str(path)


def _obj(name, ra, dec, mag=1.0, kind="star"):
    return {"name": name, "kind": kind, "mag": mag, "ra": ra, "dec": dec}


def _with_candidates(monkeypatch, objs):
    monkeypatch.setattr(beyond, "candidates", lambda exif_info: list(objs))


def test_star_just_off_the_left_edge(equator_wcs_file, monkeypatch):
    _with_candidates(monkeypatch, [_obj("Off", 103.0, 0.0)])
    out = beyond.annotate(equator_wcs_file, WIDTH, HEIGHT, {})
    assert len(out) == 1
    p = out[0]
    assert p["name"] == "Off" and p["kind"] == "star" and p["mag"] == 1.0
    assert p["side"] == "left"
    assert p["edge_x"] == 0.0
    assert p["edge_y"] == pytest.approx(HEIGHT / 2, abs=PX)
    assert p["ux"] == pytest.approx(-1.0, abs=0.002)
    assert p["uy"] == pytest.approx(0.0, abs=0.002)
    # 13 degrees east of the centre, the left edge 9.90 degrees out: the
    # distance quoted is sky separation from the edge, not tangent-plane.
    assert p["deg"] == pytest.approx(13.0 - EDGE_X_DEG, abs=DEG)


def test_in_frame_far_and_over_the_horizon_objects_are_skipped(
        equator_wcs_file, monkeypatch):
    _with_candidates(monkeypatch, [
        _obj("Inside", 95.0, 0.0),      # in the frame: the star layer has it
        _obj("TooFar", 130.0, 0.0),     # 30 degrees past the edge
        _obj("Polar", 90.0, 80.0),      # past the tangent-plane limit
        _obj("Behind", 270.0, 0.0),     # antipode: the projection means nothing
    ])
    assert beyond.annotate(equator_wcs_file, WIDTH, HEIGHT, {}) == []


def test_distortion_does_not_put_an_in_frame_planet_in_beyond(tmp_path, monkeypatch):
    # Saturn in c98582b0: the plain TAN projection puts it above the top
    # edge, while the full SIP projection correctly includes it in labels.
    wcs = synth.make_wcs(ra=90.0, dec=0.0, fov_deg=FOV, width=WIDTH, height=HEIGHT)
    a, b = np.zeros((3, 3)), np.zeros((3, 3))
    b[0, 2] = -0.00005
    wcs.sip = Sip(a, b, None, None, wcs.wcs.crpix)
    wcs.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
    ra, dec = map(float, wcs.all_pix2world(1400.0, 10.0, 0))
    assert float(wcs.wcs_world2pix(ra, dec, 0)[1]) < 0
    path = tmp_path / "distorted.wcs"
    fits.PrimaryHDU(header=wcs.to_header(relax=True)).writeto(path)
    saturn = _obj("Saturn", ra, dec, mag=0.6, kind="planet")
    monkeypatch.setattr(ephemeris, "compute_bodies", lambda *a: [saturn])
    _with_candidates(monkeypatch, [saturn, _obj("Off", 103.0, 0.0)])
    exif = {"datetime_original": "2026:08:07 02:27:50", "offset_time_original": "-03:00"}
    (label,), _ = ephemeris.annotate_bodies(str(path), WIDTH, HEIGHT, exif)
    assert label["name"] == "Saturn" and label["y"] == pytest.approx(10, abs=0.1)
    out = beyond.annotate(str(path), WIDTH, HEIGHT, exif)
    assert [p["name"] for p in out] == ["Off"]


def test_distortion_outside_tan_inside_keeps_an_outward_pointer(tmp_path, monkeypatch):
    # The reverse boundary disagreement: the full projection puts the
    # planet above the image, but TAN puts it inside. The arrow must use
    # the outside coordinates too, or it would point back into the photo.
    wcs = synth.make_wcs(ra=90.0, dec=0.0, fov_deg=FOV, width=WIDTH, height=HEIGHT)
    a, b = np.zeros((3, 3)), np.zeros((3, 3))
    b[0, 2] = 0.00005
    wcs.sip = Sip(a, b, None, None, wcs.wcs.crpix)
    wcs.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
    ra, dec = map(float, wcs.all_pix2world(1400.0, -10.0, 0))
    tx, ty = wcs.wcs_world2pix(ra, dec, 0)
    assert 0 <= tx < WIDTH and 0 <= ty < HEIGHT
    path = tmp_path / "distorted.wcs"
    fits.PrimaryHDU(header=wcs.to_header(relax=True)).writeto(path)
    saturn = _obj("Saturn", ra, dec, mag=0.6, kind="planet")
    monkeypatch.setattr(ephemeris, "compute_bodies", lambda *a: [saturn])
    _with_candidates(monkeypatch, [saturn])
    exif = {"datetime_original": "2026:08:07 02:27:50", "offset_time_original": "-03:00"}
    labels, _ = ephemeris.annotate_bodies(str(path), WIDTH, HEIGHT, exif)
    assert labels == []
    (p,) = beyond.annotate(str(path), WIDTH, HEIGHT, exif)
    assert p["name"] == "Saturn" and p["side"] == "above"
    assert p["edge_y"] == 0.0
    assert p["edge_x"] == pytest.approx(1000 + 400 * 750 / 760, abs=0.1)
    assert p["ux"] > 0 and p["uy"] < 0
    assert math.hypot(p["ux"], p["uy"]) == pytest.approx(1.0, abs=0.002)
    assert p["deg"] == pytest.approx(0.1, abs=0.01)


@pytest.mark.parametrize("failure", ["exception", "nan", "infinity"])
def test_unusable_full_projection_falls_back_to_tan(equator_wcs_file, monkeypatch, failure):
    def unusable(self, *args):
        if failure == "exception":
            raise ValueError("SIP inversion failed")
        return (math.nan, math.nan) if failure == "nan" else (math.inf, 20.0)

    monkeypatch.setattr(WCS, "all_world2pix", unusable)
    _with_candidates(monkeypatch, [_obj("Inside", 95.0, 0.0), _obj("Off", 103.0, 0.0)])
    (p,) = beyond.annotate(equator_wcs_file, WIDTH, HEIGHT, {})
    assert p["name"] == "Off" and p["side"] == "left"
    assert p["ux"] == pytest.approx(-1.0, abs=0.002)
    assert p["deg"] == pytest.approx(13.0 - EDGE_X_DEG, abs=DEG)


def test_sides_are_the_photos_not_the_skys(equator_wcs_file, monkeypatch):
    # Dec grows downward in this WCS, so a southern object is "above" the
    # frame and a northern one "below": the words follow the image as
    # displayed, which is what the person holding the phone means.
    _with_candidates(monkeypatch, [
        _obj("South", 90.0, -10.0), _obj("North", 90.0, 12.0), _obj("West", 78.0, 0.0),
    ])
    out = {p["name"]: p for p in beyond.annotate(equator_wcs_file, WIDTH, HEIGHT, {})}
    assert out["South"]["side"] == "above" and out["South"]["edge_y"] == 0.0
    assert out["South"]["deg"] == pytest.approx(10.0 - EDGE_Y_DEG, abs=DEG)
    assert out["North"]["side"] == "below" and out["North"]["edge_y"] == HEIGHT
    assert out["West"]["side"] == "right" and out["West"]["edge_x"] == WIDTH
    assert out["West"]["deg"] == pytest.approx(12.0 - EDGE_X_DEG, abs=DEG)
    assert out["West"]["ux"] == pytest.approx(1.0, abs=0.002)
    assert out["West"]["uy"] == pytest.approx(0.0, abs=0.002)


def test_a_corner_object_leaves_by_the_edge_the_ray_meets_first(
        equator_wcs_file, monkeypatch):
    # Up-left of the frame, but steeper than the frame's diagonal: the ray
    # from the centre hits the top edge before the left one.
    _with_candidates(monkeypatch, [_obj("Corner", 96.0, -12.0)])
    (p,) = beyond.annotate(equator_wcs_file, WIDTH, HEIGHT, {})
    assert p["side"] == "above" and p["edge_y"] == 0.0
    assert 0 < p["edge_x"] < WIDTH / 2
    assert p["ux"] < 0 and p["uy"] < 0
    assert math.hypot(p["ux"], p["uy"]) == pytest.approx(1.0, abs=0.002)


def test_notable_first_then_caps_per_side_and_overall(equator_wcs_file, monkeypatch):
    stars = [_obj(f"L{i}", 102.0 + i * 0.3, (i - 3) * 0.6, mag=i * 0.1)
             for i in range(6)]  # six bright stars just off the left edge
    _with_candidates(monkeypatch, stars + [
        _obj("Moon", 90.0, -9.5, mag=-12.0, kind="moon"),
        _obj("Mars", 90.0, -11.0, mag=1.5, kind="planet"),
        _obj("Pleiades (M45)", 90.0, 11.0, mag=1.6, kind="dso"),
    ])
    out = beyond.annotate(equator_wcs_file, WIDTH, HEIGHT, {})
    assert len(out) == beyond.MAX_POINTERS
    # the Moon, planets and DSOs lead regardless of magnitude
    assert [p["name"] for p in out[:3]] == ["Moon", "Mars", "Pleiades (M45)"]
    lefts = [p["name"] for p in out if p["side"] == "left"]
    assert len(lefts) == beyond.MAX_PER_SIDE
    assert lefts == ["L0", "L1", "L2"]  # the brightest three of the six


def test_candidates_take_the_bright_end_of_each_catalog(mini_catalog, mini_dso_catalog):
    names = {(c["kind"], c["name"]) for c in beyond.candidates({})}
    stars = {n for k, n in names if k == "star"}
    dsos = {n for k, n in names if k == "dso"}
    # the star cut is magnitude 2; the mini catalog's fainter rows drop out
    assert stars and all(
        s["mag"] <= beyond.STAR_MAG_LIMIT for s in beyond.candidates({})
        if s["kind"] == "star")
    # only the household-name showpieces, never an anonymous cluster
    assert dsos <= set(beyond.dso.DISPLAY_NAMES.values())
    assert "Pleiades (M45)" in dsos
    # no timestamp, no bodies
    assert not any(k in ("moon", "planet") for k, _ in names)


@pytest.mark.skipif(not os.path.exists(os.path.join(synth.CATALOG_DIR, "hyg.csv")),
                    reason="needs the real catalogs")
def test_real_catalog_run_is_cheap_and_sane(tmp_path):
    # A 20x15 degree field between Orion and Canis Minor: Procyon sits
    # about ten degrees past the left edge.
    wcs = synth.make_wcs(ra=95.0, dec=5.0, fov_deg=FOV, width=WIDTH, height=HEIGHT)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    # The catalog parses are paid by the star and DSO layers before this
    # one runs in the worker (and cached), so time the layer's own cost.
    beyond.solver.load_catalog()
    beyond.dso.load_catalog()
    t0 = time.perf_counter()
    out = beyond.annotate(str(path), WIDTH, HEIGHT, {})
    elapsed = time.perf_counter() - t0
    print(f"\nbeyond.annotate on the real catalog, catalogs warm: "
          f"{elapsed * 1000:.1f} ms, {len(out)} pointers: "
          + ", ".join(f"{p['name']} {p['deg']}° {p['side']}" for p in out))
    assert elapsed < 0.5
    assert "Procyon" in {p["name"] for p in out}
    for p in out:
        assert p["deg"] <= beyond.MAX_EDGE_DEG
        assert p["edge_x"] in (0.0, WIDTH) or p["edge_y"] in (0.0, HEIGHT)
        assert math.hypot(p["ux"], p["uy"]) == pytest.approx(1.0, abs=0.002)


def test_format_deg_matches_the_page():
    # A tenth under one degree ("just past the edge", not "0°"), whole
    # degrees above, ties half-up like Math.round — Python's own round
    # would send 8.5 to 8 and put the card at odds with the page.
    assert beyond.format_deg(0.4) == "0.4°"
    assert beyond.format_deg(8.5) == "9°"
    assert beyond.format_deg(11.6) == "12°"
    assert beyond.format_deg(1.0) == "1°"
    assert round(8.5) == 8  # the trap this exists to avoid
