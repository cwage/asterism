"""The satellite layer against pixels, end to end (#11).

`app.satellites` deliberately does not detect streaks — it propagates
element sets and projects the track, and the UI draws that dashed to say
so. Nothing therefore checks the computed track against an actual streak,
which is where a sign error, a parallax mistake or an off-by-one exposure
window would hide: every existing test asks the same code both questions.

This renders a frame containing a streak drawn from a *real* ISS element
set, plate-solves it for real, and asks the satellite layer where the ISS
was. Agreement is only possible if the propagation, the observer parallax,
the exposure window and the projection are all right.

Marked `solver`: needs solve-field and the wide-field indexes.
    docker compose run --rm worker pytest -m solver tests/test_satellite_streak.py
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app import db, exif, ephemeris, satellites, solver
from tests import synth

pytestmark = pytest.mark.solver

# A real ISS element set, fetched from Celestrak 2026-08-17. Epoch is
# 2026 day 228.567 (Aug 16, 13:36 UTC), so the pass below sits within a
# day of epoch, where SGP4 is at its most accurate.
TLE_1 = "1 25544U 98067A   26228.56710022  .00005115  00000+0  99348-4 0  9991"
TLE_2 = "2 25544  51.6334   1.2594 0007609  53.1141 307.0544 15.49461657581119"
NORAD = "25544"

# A genuine high pass over Nashville: found by propagating the element set
# above and keeping the times when the ISS was over 40 degrees altitude
# with a star-rich field behind it. 56 degrees up, 133 catalogue stars
# within the frame, 04:56 local — properly dark.
LAT, LON = 36.16, -86.78
WHEN = datetime(2026, 8, 17, 9, 56, 0, tzinfo=timezone.utc)
# What the phone would have written: local clock plus its UTC offset.
LOCAL_CLOCK = "2026:08:17 04:56:00"
UTC_OFFSET = "-05:00"
EXPOSURE = 10.0
WIDTH, HEIGHT = 1600, 1200
FOV_DEG = 50.0


def _iss():
    from skyfield.api import EarthSatellite
    return EarthSatellite(TLE_1, TLE_2, "ISS (ZARYA)", ephemeris._timescale())


def _track(sat, start, seconds, samples=25):
    """Apparent (ra, dec) of `sat` from the site, across the exposure."""
    from skyfield.api import wgs84
    ts = ephemeris._timescale()
    site = wgs84.latlon(LAT, LON)
    out = []
    for i in range(samples):
        when = start + timedelta(seconds=seconds * i / (samples - 1))
        ra, dec, _ = (sat - site).at(ts.from_datetime(when)).radec()
        out.append((ra.hours * 15.0, dec.degrees))
    return out


@pytest.fixture()
def streaked_frame(tmp_path, monkeypatch):
    """A solvable star field with the ISS drawn across it, plus the EXIF a
    phone would have written. Returns (path, truth_wcs, track)."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    track = _track(_iss(), WHEN, EXPOSURE)
    mid = track[len(track) // 2]
    path = tmp_path / "iss.jpg"
    truth = synth.render_starfield(
        str(path), ra=mid[0], dec=mid[1], fov_deg=FOV_DEG,
        width=WIDTH, height=HEIGHT, trails=[track],
        exif=synth.build_exif(
            f35mm=39, datetime_original=LOCAL_CLOCK,
            offset_time_original=UTC_OFFSET,
            gps=(LAT, LON), exposure_seconds=EXPOSURE),
    )
    return str(path), truth, track


def _distance_to_polyline(point, verts):
    """Shortest distance from a point to the polyline through `verts`.

    To the *segments*, not to the vertices: the drawn samples sit about
    11px apart, so a nearest-vertex measure would report up to ~5px of
    its own discretisation and drown the thing being measured.
    """
    px, py = point
    best = float("inf")
    for (x0, y0), (x1, y1) in zip(verts, verts[1:]):
        dx, dy = x1 - x0, y1 - y0
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else ((px - x0) * dx + (py - y0) * dy) / span
        t = max(0.0, min(1.0, t))
        best = min(best, math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)))
    return best


def _stub_fetch(url, user, password):
    return [{"NORAD_CAT_ID": NORAD, "OBJECT_NAME": "ISS (ZARYA)",
             "EPOCH": "2026-08-16T13:36:37", "TLE_LINE1": TLE_1,
             "TLE_LINE2": TLE_2}]


def test_the_streak_is_where_the_satellite_layer_says_it_is(
        streaked_frame, tmp_path, monkeypatch):
    monkeypatch.setenv("SPACETRACK_USER", "user@example.com")
    monkeypatch.setenv("SPACETRACK_PASS", "hunter2")
    satellites._sat_cache.clear()
    path, truth, track = streaked_frame

    info = exif.read_exif(path)
    assert info["lat"] == pytest.approx(LAT, abs=0.01)
    assert info["exposure_seconds"] == pytest.approx(EXPOSURE, abs=0.01)
    # The instant has to come back exactly: a low pass moves about a degree
    # a second, so a zone guess that is an hour out puts the ISS in a
    # different constellation.
    when, source = ephemeris.resolve_utc(info)
    assert source == "exif_offset"
    assert when == WHEN

    result = solver.solve_tiered(path, str(tmp_path / "out"), info)
    assert result["success"], result["log_tail"]

    layer = satellites.annotate(result["wcs_path"], WIDTH, HEIGHT, info,
                                fetch=_stub_fetch)
    crossings = layer["crossings"]
    assert len(crossings) == 1, [c["name"] for c in crossings]
    assert crossings[0]["norad_id"] == NORAD

    # Where the streak was actually drawn, in the *solved* frame's pixels.
    drawn_x, drawn_y = truth.all_world2pix([p[0] for p in track],
                                           [p[1] for p in track], 0)
    drawn = np.column_stack([drawn_x, drawn_y])

    # Every computed sample must land on the drawn line. Distance to the
    # line rather than sample-to-sample: the layer chooses its own
    # sampling rate, and the claim under test is "the track lies along the
    # streak", not "the two loops happened to step in lockstep".
    verts = [tuple(v) for v in drawn]
    worst = max(_distance_to_polyline(p, verts) for p in crossings[0]["points"])
    # Measured at 0.16px worst (0.11 median) on this frame. The bound has
    # room for solve-to-solve variation and is still brutally tight in the
    # units that matter: the streak crosses 26.7px per second of exposure,
    # so 2px is about 75 milliseconds. A sign error in the parallax, a
    # mis-centred exposure window or a stale element set cannot survive it.
    assert worst < 2.0, f"computed track strays {worst:.2f}px from the streak"


def test_the_streak_does_not_stop_the_frame_solving(streaked_frame, tmp_path):
    """A satellite trail is a bright line across the star pattern. It must
    not be mistaken for stars badly enough to break the match — the whole
    layer is moot on a frame that no longer solves."""
    path, truth, _ = streaked_frame
    info = exif.read_exif(path)
    result = solver.solve_tiered(path, str(tmp_path / "out"), info)
    assert result["success"], result["log_tail"]

    from astropy.io import fits
    from astropy.wcs import WCS
    with fits.open(result["wcs_path"]) as hdul:
        solved = WCS(hdul[0].header)
    ra_t, dec_t = truth.all_pix2world(WIDTH / 2, HEIGHT / 2, 0)
    ra_s, dec_s = solved.all_pix2world(WIDTH / 2, HEIGHT / 2, 0)
    sep = math.hypot((float(ra_s) - float(ra_t)) *
                     math.cos(math.radians(float(dec_t))),
                     float(dec_s) - float(dec_t))
    assert sep < 0.5, f"solved centre is {sep:.3f} deg from the truth"
