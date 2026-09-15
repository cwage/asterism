"""Where on Earth (#115), from the phone's tilt. The frame mapping is
pinned case by case, the great-circle step on a synthetic WCS with a
zenith placed by hand, and the whole chain on a constructed Sydney
evening whose answer is a suburb name off the vendored map."""

import math
import os
from datetime import datetime, timezone

import pytest
from astropy.io import fits

from app import ephemeris, locate
from tests import synth

needs_maps = pytest.mark.skipif(
    not all(os.path.exists(os.path.join(locate.CATALOG_DIR, f))
            for f in (locate.COUNTRIES_FILE, locate.ADMIN1_FILE)),
    reason="Natural Earth maps not fetched (scripts/fetch-catalog.sh)",
)

WIDTH, HEIGHT = 1500, 2000


def _portrait_gravity(zeta_deg, lean_deg=0.0):
    """Gravity for a phone held upright in portrait, the rear camera
    aimed `zeta_deg` below the zenith, rolled `lean_deg` (positive: the
    top of the phone leans to its right)."""
    z = math.radians(zeta_deg)
    r = math.radians(lean_deg)
    # up in phone coordinates: mostly toward the top (+Y), tipped toward
    # the camera axis (-Z) by the aim, and toward +X by the lean
    up = (math.sin(z) * math.sin(r), math.sin(z) * math.cos(r), -math.cos(z))
    return [-v for v in up]


# ---- the frame mapping ----

def _held(zeta_deg, up_axis):
    """Gravity for a phone whose `up_axis` (a unit vector in the phone's
    frame, e.g. (0, 1, 0) for the top of the phone) points at the sky,
    the rear camera aimed `zeta_deg` below the zenith."""
    z = math.radians(zeta_deg)
    ux, uy, _ = up_axis
    up = (math.sin(z) * ux, math.sin(z) * uy, -math.cos(z))
    return [-v for v in up]


def test_upright_portrait_puts_the_zenith_straight_up_the_frame():
    (dx, dy), zeta = locate.up_in_frame(_portrait_gravity(30.0), WIDTH, HEIGHT)
    assert (dx, dy) == pytest.approx((0.0, -1.0), abs=1e-9)
    assert zeta == pytest.approx(30.0)


def test_the_hold_is_read_from_the_tilt_and_the_frame_shape():
    # However the phone was held, the zenith ends up straight up the
    # upright frame, with no orientation tag consulted: a tool that
    # rotated the pixels and reset the tag changes nothing.
    portrait = {(0, 1, 0): "portrait", (0, -1, 0): "portrait-inverted"}
    landscape = {(1, 0, 0): "landscape-right", (-1, 0, 0): "landscape-left"}
    for axis, name in portrait.items():
        g = _held(40.0, axis)
        assert locate.hold([-v for v in g], WIDTH, HEIGHT) == name
        (dx, dy), zeta = locate.up_in_frame(g, WIDTH, HEIGHT)
        assert (dx, dy) == pytest.approx((0.0, -1.0), abs=1e-9), name
        assert zeta == pytest.approx(40.0)
    for axis, name in landscape.items():
        g = _held(40.0, axis)
        assert locate.hold([-v for v in g], HEIGHT, WIDTH) == name
        (dx, dy), _ = locate.up_in_frame(g, HEIGHT, WIDTH)
        assert (dx, dy) == pytest.approx((0.0, -1.0), abs=1e-9), name
    # a square crop goes by whichever phone axis is nearer the sky
    assert locate.hold((0.2, 0.9, -0.4), 1000, 1000) == "portrait"
    assert locate.hold((0.2, -0.9, -0.4), 1000, 1000) == "portrait-inverted"
    assert locate.hold((0.9, 0.2, -0.4), 1000, 1000) == "landscape-right"


def test_a_lean_tips_the_zenith_across_the_frame():
    (dx, dy), _ = locate.up_in_frame(_portrait_gravity(45.0, lean_deg=20.0), WIDTH, HEIGHT)
    assert dy < 0 and abs(dx) == pytest.approx(math.sin(math.radians(20.0)), abs=1e-6)


def test_aimed_at_the_ground_is_no_sky_photo():
    assert locate.up_in_frame([0.0, -1.0, 0.0], WIDTH, HEIGHT) is None  # horizontal, edge case
    assert locate.up_in_frame([0.0, -0.5, -0.866], WIDTH, HEIGHT) is None  # aimed down
    assert locate.up_in_frame([0.0, 0.0, 0.0], WIDTH, HEIGHT) is None
    # straight up: a direction is still returned so the caller need not care
    assert locate.up_in_frame([0.0, 0.0, 1.0], WIDTH, HEIGHT)[1] == pytest.approx(0.0)


# ---- the great-circle step ----

@pytest.fixture()
def wcs_file(tmp_path):
    # synth.make_wcs has Dec growing downward: image up is south.
    wcs = synth.make_wcs(ra=90.0, dec=0.0, fov_deg=40.0, width=WIDTH, height=HEIGHT)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    return wcs


def test_zenith_walks_the_great_circle_from_the_centre(wcs_file):
    ra, dec = locate.zenith(wcs_file, WIDTH, HEIGHT, _portrait_gravity(30.0))
    assert ra == pytest.approx(90.0, abs=0.05)
    assert dec == pytest.approx(-30.0, abs=0.05)  # 30 degrees up the frame = south
    # far beyond the frame's edge too: the step is on the sphere, not the pixels
    ra, dec = locate.zenith(wcs_file, WIDTH, HEIGHT, _portrait_gravity(80.0))
    assert (ra, dec) == pytest.approx((90.0, -80.0), abs=0.05)


# ---- the whole chain ----

def _exif(when_local, offset, gravity, orientation=6, **more):
    return {"width": WIDTH, "height": HEIGHT, "datetime_original": when_local,
            "offset_time_original": offset, "gravity": gravity,
            "orientation": orientation, "lat": None, "lon": None, **more}


def _wcs_with_zenith_at(tmp_path, lat, lon, when_utc, zeta=30.0):
    """A frame whose centre is `zeta` degrees down the frame (north, in
    this WCS) from the zenith over (lat, lon) at `when_utc`."""
    gmst = float(ephemeris._timescale().from_datetime(when_utc).gmst) * 15.0
    ra = (gmst + lon) % 360.0
    wcs = synth.make_wcs(ra=ra, dec=lat + zeta, fov_deg=40.0, width=WIDTH, height=HEIGHT)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    return str(path)


@needs_maps
def test_a_sydney_evening_comes_back_as_new_south_wales(tmp_path):
    when_utc = datetime(2025, 8, 20, 11, 30, tzinfo=timezone.utc)  # 21:30 AEST
    wcs_path = _wcs_with_zenith_at(tmp_path, -33.9, 151.2, when_utc)
    place = locate.annotate(wcs_path, WIDTH, HEIGHT,
                            _exif("2025:08:20 21:30:00", "+10:00", _portrait_gravity(30.0)))
    assert place["source"] == "tilt"
    assert place["lat"] == pytest.approx(-33.9, abs=0.2)
    assert place["lon"] == pytest.approx(151.2, abs=0.2)
    assert place["zone"] == "Australia/Sydney"
    assert "New South Wales, in Australia" in place["regions"]
    assert place["line"] == ("The phone recorded its tilt, so sky geometry puts this near "
                             f"34°S, 151°E: {place['regions']}.")


@needs_maps
def test_a_clock_that_disagrees_with_the_tilt_says_nothing(tmp_path):
    when_utc = datetime(2025, 8, 20, 11, 30, tzinfo=timezone.utc)
    wcs_path = _wcs_with_zenith_at(tmp_path, -33.9, 151.2, when_utc)
    # the same sky, but a clock claiming UTC+5: no such place has Sydney's sky then
    place = locate.annotate(wcs_path, WIDTH, HEIGHT,
                            _exif("2025:08:20 16:30:00", "+05:00", _portrait_gravity(30.0)))
    assert place is None
    assert locate.estimate(wcs_path, WIDTH, HEIGHT,
                           _exif("2025:08:20 16:30:00", "+05:00", _portrait_gravity(30.0)))["reason"] \
        == "clock disagrees with the tilt"


@needs_maps
def test_a_gps_fix_is_just_named(tmp_path):
    place = locate.annotate(None, WIDTH, HEIGHT, {"lat": 36.16, "lon": -86.78})
    assert place["source"] == "gps"
    assert place["line"] == "Photographed from Tennessee, in the United States."


def test_no_tilt_and_no_gps_is_nothing(tmp_path):
    assert locate.annotate(None, WIDTH, HEIGHT, {"lat": None, "lon": None, "gravity": None}) is None


def test_no_clock_offset_keeps_the_latitude_but_says_nothing(tmp_path):
    when_utc = datetime(2025, 8, 20, 11, 30, tzinfo=timezone.utc)
    wcs_path = _wcs_with_zenith_at(tmp_path, -33.9, 151.2, when_utc)
    out = locate.estimate(wcs_path, WIDTH, HEIGHT,
                          _exif("2025:08:20 21:30:00", None, _portrait_gravity(30.0)))
    assert out["lat"] == pytest.approx(-33.9, abs=0.2)
    assert out["reason"] == "no clock offset"
    assert locate.describe(out) is None


@needs_maps
def test_names_read_like_a_person_wrote_them():
    phrase, sea = locate.name_box(36.3, 74.5, 2.5, 3.0)  # Hunza
    assert "Pakistan" in phrase and sea < 0.5
    assert phrase.startswith("northern Pakistan")
    phrase, _ = locate.name_box(36.2, -86.8, 2.5, 3.0)  # Nashville
    assert phrase.startswith("Tennessee") and phrase.endswith(", in the United States")
    phrase, sea = locate.name_box(-45.0, -30.0, 1.0, 1.0)  # South Atlantic
    assert phrase is None and sea == 1.0


def test_describe_formats_each_hemisphere():
    place = {"source": "tilt", "lat": -33.9, "lon": 151.2, "regions": "New South Wales, in Australia",
             "reason": None}
    assert locate.describe(place).endswith("near 34°S, 151°E: New South Wales, in Australia.")
    place.update(lat=36.2, lon=-86.8, regions=None)
    assert locate.describe(place).endswith("near 36°N, 87°W: open water on this map.")
    assert locate.describe({"source": "gps", "regions": None, "reason": None}) is None
