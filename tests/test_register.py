"""Registering a frame from Moon/planet positions (#85).

The geometry here is exact and tested against known WCSs. Identification —
deciding which blob *is* the Moon — is not solved; see the measurements on the
issue. These cover the parts that work.
"""

import numpy as np
import pytest

from app import register
from tests import synth

W, H = 4000, 3000


def _sky(wcs, x, y):
    ra, dec = wcs.all_pix2world(x, y, 0)
    return float(ra), float(dec)


@pytest.mark.parametrize("fov,ra,dec", [
    (39.3, 200.0, -5.0),     # the real dusk frame's field, roughly
    (70.0, 95.0, -10.0),     # an unzoomed phone frame
    (12.0, 45.0, 60.0),      # telephoto
], ids=["zoomed", "wide", "telephoto"])
def test_recovers_a_known_wcs_from_two_points(fov, ra, dec):
    truth = synth.make_wcs(ra, dec, fov, W, H)
    p1, p2 = (1614.0, 813.0), (2984.0, 1686.0)
    got = register.wcs_from_pair(p1, _sky(truth, *p1), p2, _sky(truth, *p2), W, H)
    assert got is not None

    # Positions the fit never saw must land where the truth puts them.
    gx, gy = np.meshgrid(np.linspace(200, W - 200, 5), np.linspace(200, H - 200, 5))
    world = truth.all_pix2world(np.c_[gx.ravel(), gy.ravel()], 0)
    back = got.all_world2pix(world, 0)
    err = np.hypot(back[:, 0] - gx.ravel(), back[:, 1] - gy.ravel())
    assert err.max() < 2.0, f"max {err.max():.2f} px across the frame"


def test_anchors_land_exactly():
    truth = synth.make_wcs(200.0, -5.0, 39.3, W, H)
    p1, p2 = (1614.0, 813.0), (2984.0, 1686.0)
    s1, s2 = _sky(truth, *p1), _sky(truth, *p2)
    got = register.wcs_from_pair(p1, s1, p2, s2, W, H)
    assert max(register.pair_residuals(got, [(p1, s1), (p2, s2)])) < 0.05


def test_reference_sits_at_the_frame_centre():
    # A lens is gnomonic about its optical axis, so the tangent point belongs
    # at the centre of the frame. Referencing it at the pair's midpoint instead
    # reproduces both anchors and still drifts 246 px at the edge of a 70
    # degree field.
    truth = synth.make_wcs(95.0, -10.0, 70.0, W, H)
    p1, p2 = (1000.0, 700.0), (3000.0, 2000.0)
    got = register.wcs_from_pair(p1, _sky(truth, *p1), p2, _sky(truth, *p2), W, H)
    assert got.wcs.crpix[0] == pytest.approx(W / 2.0 + 1.0, abs=1.0)
    assert got.wcs.crpix[1] == pytest.approx(H / 2.0 + 1.0, abs=1.0)


def test_parity_is_right_way_round():
    # Getting the sky-image parity flip backwards puts every label exactly one
    # baseline away — the failure mode looks like a plausible fit rather than
    # an error, which is why it has its own test.
    truth = synth.make_wcs(200.0, -5.0, 39.3, W, H)
    p1, p2 = (1614.0, 813.0), (2984.0, 1686.0)
    got = register.wcs_from_pair(p1, _sky(truth, *p1), p2, _sky(truth, *p2), W, H)
    ra_t, dec_t = truth.all_pix2world(W / 2, H / 2, 0)
    ra_g, dec_g = got.all_pix2world(W / 2, H / 2, 0)
    assert register.angular_separation(float(ra_t), float(dec_t),
                                       float(ra_g), float(dec_g)) < 0.05


def test_pairs_too_close_together_are_refused():
    truth = synth.make_wcs(200.0, -5.0, 39.3, W, H)
    p1, p2 = (2000.0, 1500.0), (2010.0, 1505.0)   # ~0.1 deg apart
    assert register.wcs_from_pair(p1, _sky(truth, *p1), p2, _sky(truth, *p2),
                                  W, H) is None


def test_angular_separation_matches_known_values():
    assert register.angular_separation(0, 0, 0, 90) == pytest.approx(90.0)
    assert register.angular_separation(0, 0, 180, 0) == pytest.approx(180.0)
    assert register.angular_separation(10, 20, 10, 20) == pytest.approx(0.0, abs=1e-9)


# ---- detection ----

def test_detector_finds_a_point_and_an_extended_source(tmp_path):
    path = tmp_path / "sky.jpg"
    # A "planet" (point) and a "Moon" (wide blob) on smooth sky.
    synth.render_points(str(path), [(600, 400), (1000, 700)], width=1600,
                        height=1200, amps=[200.0, 60.0],
                        blobs=[(1000, 700, 170.0, 9.0)])
    found = register.detect_sources(str(path), factor=2)
    assert found, "should find the two planted sources"

    def near(x, y):
        return [s for s in found if abs(s["x"] - x) < 25 and abs(s["y"] - y) < 25]
    point, blob = near(600, 400), near(1000, 700)
    assert point, f"missed the point source; found {[(s['x'], s['y']) for s in found]}"
    assert blob, "missed the extended source"
    assert blob[0]["extent"] > point[0]["extent"], \
        "the extended source must measure wider — that is what identifies a Moon"


def test_detector_survives_a_blank_frame(tmp_path):
    path = tmp_path / "black.jpg"
    synth.render_black(str(path), width=800, height=600)
    assert register.detect_sources(str(path), factor=2) == []


def test_skyline_puts_smooth_sky_above_textured_ground(tmp_path):
    path = tmp_path / "scene.jpg"
    # Smooth top half, noisy bottom half — a crude horizon.
    from PIL import Image
    rng = np.random.default_rng(3)
    arr = np.full((1200, 1600), 40.0)
    arr[700:] += rng.normal(0, 45, (500, 1600))
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB").save(path)

    skyline, tile = register.sky_mask(str(path), factor=2)
    assert 500 < float(np.median(skyline)) < 900, f"got {np.median(skyline)}"
    assert register.in_sky({"x": 800.0, "y": 300.0}, skyline, tile)
    assert not register.in_sky({"x": 800.0, "y": 1100.0}, skyline, tile)


# ---- identifying which blob is which ----

MOON = {"name": "Moon", "ra": 190.0, "dec": -5.0, "kind": "moon"}
VENUS = {"name": "Venus", "ra": 176.0, "dec": 2.0, "kind": "planet"}


def _frame_with_bodies(path, wcs, moon_px, venus_px, sun_px, width, height):
    """A synthetic sky: a crescent-ish Moon lit towards the Sun, a bright
    planet, and some decoy blobs of the wrong size."""
    from PIL import Image
    rng = np.random.default_rng(5)
    arr = np.full((height, width), 12.0) + rng.normal(0, 1.2, (height, width))

    yy, xx = np.mgrid[0:height, 0:width]
    # Moon: a disc with its far side dimmed, so the bright side faces the Sun.
    r = 26.0
    d = np.hypot(xx - moon_px[0], yy - moon_px[1])
    disc = np.clip(1.0 - (d / r) ** 6, 0, 1)
    towards = np.hypot(*(np.array(sun_px) - np.array(moon_px)))
    ux = (sun_px[0] - moon_px[0]) / towards
    uy = (sun_px[1] - moon_px[1]) / towards
    lit = np.clip(((xx - moon_px[0]) * ux + (yy - moon_px[1]) * uy) / r, -1, 1)
    arr += disc * (60 + 150 * np.clip(lit, 0, 1))

    synth.stamp_stars(arr, [(venus_px[0], venus_px[1], 220.0)], sigma=2.0)
    # Decoys: round, evenly lit, nothing to do with the Sun.
    for dx, dy in ((400, 300), (1200, 2400), (3400, 500)):
        dd = np.hypot(xx - dx, yy - dy)
        arr += np.clip(1.0 - (dd / 30.0) ** 6, 0, 1) * 170
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB").save(path)


def test_identifies_the_moon_by_the_direction_its_limb_faces(tmp_path):
    path = tmp_path / "dusk.jpg"
    truth = synth.make_wcs(183.0, -1.5, 39.3, W, H)
    moon_px = tuple(float(v) for v in truth.all_world2pix(MOON["ra"], MOON["dec"], 0))
    venus_px = tuple(float(v) for v in truth.all_world2pix(VENUS["ra"], VENUS["dec"], 0))
    sun = (168.0, 8.0)
    sun_px = tuple(float(v) for v in truth.all_world2pix(*sun, 0))
    _frame_with_bodies(str(path), truth, moon_px, venus_px, sun_px, W, H)

    exif_info = {"width": W, "height": H, "fov_bounds": (25.8, 88.5)}
    best = register.register_frame(str(path), exif_info, [MOON, VENUS], sun)
    assert best is not None, "should register from the two bodies"
    assert best["bodies"] == ["Moon", "Venus"]

    got = dict(zip(best["bodies"], best["sources"]))
    assert abs(got["Moon"]["x"] - moon_px[0]) < 25
    assert abs(got["Venus"]["x"] - venus_px[0]) < 25
    assert best["field_deg"] == pytest.approx(39.3, rel=0.15)
    assert best["limb_offset_deg"] < register.MAX_LIMB_DISAGREEMENT_DEG


def test_evenly_lit_blobs_are_not_mistaken_for_a_moon(tmp_path):
    """The decoys are the same size and brighter — only the lit-side geometry
    tells them apart, which is the whole point of the limb check."""
    path = tmp_path / "decoys.jpg"
    truth = synth.make_wcs(183.0, -1.5, 39.3, W, H)
    venus_px = tuple(float(v) for v in truth.all_world2pix(VENUS["ra"], VENUS["dec"], 0))
    sun = (168.0, 8.0)
    from PIL import Image
    rng = np.random.default_rng(6)
    arr = np.full((H, W), 12.0) + rng.normal(0, 1.2, (H, W))
    yy, xx = np.mgrid[0:H, 0:W]
    for dx, dy in ((900, 900), (2600, 1500), (1800, 2200)):
        d = np.hypot(xx - dx, yy - dy)
        arr += np.clip(1.0 - (d / 26.0) ** 6, 0, 1) * 200      # round, uniform
    synth.stamp_stars(arr, [(venus_px[0], venus_px[1], 220.0)], sigma=2.0)
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB").save(path)

    exif_info = {"width": W, "height": H, "fov_bounds": (25.8, 88.5)}
    assert register.register_frame(str(path), exif_info, [MOON, VENUS], sun) is None


def test_a_pair_without_the_moon_is_refused(tmp_path):
    # Two planets carry no check on their own identification, so registration
    # declines rather than guessing.
    path = tmp_path / "planets.jpg"
    synth.render_points(str(path), [(1000, 800), (2600, 1900)], width=W, height=H,
                        amps=[220.0, 190.0])
    jupiter = {"name": "Jupiter", "ra": 176.0, "dec": 2.0, "kind": "planet"}
    exif_info = {"width": W, "height": H, "fov_bounds": (25.8, 88.5)}
    assert register.register_frame(str(path), exif_info,
                                   [VENUS, jupiter], (168.0, 8.0)) is None


def test_registration_needs_two_bodies():
    exif_info = {"width": W, "height": H, "fov_bounds": (25.8, 88.5)}
    assert register.register_frame("unused.jpg", exif_info, [MOON], (168.0, 8.0)) is None
