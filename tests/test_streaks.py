"""Streak detection and classification against synthetic frames with known
ground truth: no solver involved, the WCS is handed in."""

import math
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image

from app import streaks
from tests import synth

WIDTH, HEIGHT = 1200, 900
STARS = [(150.0, 120.0), (600.0, 100.0), (1050.0, 150.0), (120.0, 450.0),
         (580.0, 420.0), (1000.0, 480.0), (180.0, 780.0), (620.0, 800.0),
         (1020.0, 760.0), (380.0, 260.0), (820.0, 620.0), (400.0, 620.0),
         (700.0, 300.0), (300.0, 700.0), (900.0, 250.0)]


def render(path, trail=None, profile=None, amp=60.0, stars=STARS, edge=False):
    """A star field with an optional streak from trail[0] to trail[1].
    `profile(f)` scales the amplitude along the streak (f in 0..1) so a
    meteor's fade-in can be drawn; None draws a uniform satellite trail.
    `edge` blacks out the bottom third: a roofline against the sky."""
    rng = np.random.default_rng(3)
    arr = np.full((HEIGHT, WIDTH), 40.0)
    arr += rng.normal(0.0, 2.0, arr.shape)
    synth.stamp_stars(arr, [(x, y, 180.0) for x, y in stars])
    if trail:
        (x0, y0), (x1, y1) = trail
        n = int(math.hypot(x1 - x0, y1 - y0) / 0.5)
        samples = []
        for i in range(n + 1):
            f = i / n
            a = amp * (profile(f) if profile else 1.0)
            samples.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, a * 0.5))
        synth.stamp_stars(arr, samples)
    if edge:
        arr[600:, :] = 0.0
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB") \
        .save(path, quality=92)


def meteor_profile(f):
    """Faint onset, brightening to a peak past the middle, then a stop."""
    return 0.15 + 0.85 * min(1.0, f / 0.7) ** 2


TRAIL = ((300.0, 200.0), (700.0, 500.0))
# No GPS and no zone: the satellite speed limit falls back to overhead
# pace, so the tests below control the verdict through exposure alone.
EXIF = {"width": WIDTH, "height": HEIGHT, "exposure_seconds": 30.0,
        "datetime_original": "2026:08:12 23:30:00",
        "offset_time_original": None, "lat": None, "lon": None}


def endpoints_close(streak, trail, tol=8.0):
    ends = {tuple(streak["start"]), tuple(streak["end"])}
    return all(any(math.hypot(ex - tx, ey - ty) < tol for ex, ey in ends)
               for tx, ty in trail)


def test_meteor_shaped_streak_is_found_start_first(tmp_path):
    path = str(tmp_path / "meteor.jpg")
    render(path, TRAIL, meteor_profile)
    found = streaks.detect(path)
    assert len(found["streaks"]) == 1
    s = found["streaks"][0]
    assert endpoints_close(s, TRAIL)
    # The faint end is the start.
    assert math.hypot(s["start"][0] - TRAIL[0][0], s["start"][1] - TRAIL[0][1]) < 8
    assert s["taper_start"] > s["taper_end"]
    assert s["flatness"] < streaks.METEOR_MAX_FLATNESS
    assert s["coverage"] > 0.9 and not s["dashed"]
    assert abs(s["length_px"] - 500.0) < 12


def test_uniform_trail_is_found_flat(tmp_path):
    path = str(tmp_path / "sat.jpg")
    render(path, TRAIL)
    found = streaks.detect(path)
    assert len(found["streaks"]) == 1
    s = found["streaks"][0]
    assert endpoints_close(s, TRAIL)
    assert s["flatness"] >= streaks.SATELLITE_MIN_FLATNESS
    assert max(s["taper_start"], s["taper_end"]) < streaks.SATELLITE_MAX_TAPER


def test_plain_star_field_has_no_streaks(tmp_path):
    path = str(tmp_path / "stars.jpg")
    render(path)
    assert streaks.detect(path)["streaks"] == []


def test_roofline_edge_is_not_a_streak(tmp_path):
    path = str(tmp_path / "edge.jpg")
    render(path, edge=True)
    assert streaks.detect(path)["streaks"] == []


def test_lit_rim_inside_silhouette_is_not_a_streak(tmp_path):
    # A thin bright line a few pixels inside the black foreground: the
    # classic building-edge highlight. Sky on neither side.
    path = str(tmp_path / "rim.jpg")
    render(path, ((100.0, 700.0), (1100.0, 720.0)), edge=True)
    assert streaks.detect(path)["streaks"] == []


def test_dashed_trail_is_found_and_flagged(tmp_path):
    # A stacked night mode leaves a satellite as dashes; still one line.
    path = str(tmp_path / "dashed.jpg")
    render(path, TRAIL, profile=lambda f: 1.0 if int(f * 40) % 2 == 0 else 0.0)
    found = streaks.detect(path)
    assert len(found["streaks"]) == 1
    s = found["streaks"][0]
    assert endpoints_close(s, TRAIL, tol=20.0)
    assert s["dashed"] is True


def test_regular_breaks_are_counted_and_uneven_ones_are_not(tmp_path):
    # Frame boundaries: the meteor curve interrupted at thirds.
    def broken(f):
        near = min(abs(f - 1 / 3), abs(f - 2 / 3))
        return meteor_profile(f) * (0.2 if near < 0.01 else 1.0)
    path = str(tmp_path / "broken.jpg")
    render(path, TRAIL, broken)
    s = streaks.detect(path)["streaks"][0]
    assert s["periodic_breaks"] == 2
    # One break off-centre is not a rhythm.
    def uneven(f):
        return meteor_profile(f) * (0.2 if abs(f - 0.2) < 0.01 else 1.0)
    render(path, TRAIL, uneven)
    s = streaks.detect(path)["streaks"][0]
    assert s["periodic_breaks"] == 0


# --- classification ---------------------------------------------------------

def classify(streak, fov_deg=50.0, exif=EXIF, crossings=None, when=None):
    wcs = synth.make_wcs(95.0, 40.0, fov_deg, WIDTH, HEIGHT)
    if when is None:
        when = datetime(2026, 8, 12, 23, 30, tzinfo=timezone.utc)
    return streaks.classify(dict(streak), wcs, WIDTH, exif, crossings or [], when)


def detected(tmp_path, profile=None):
    path = str(tmp_path / "frame.jpg")
    render(path, TRAIL, profile)
    return streaks.detect(path)["streaks"][0]


def test_meteor_profile_classifies_as_meteor_with_reasons(tmp_path):
    # 20 degrees in 30 seconds is within orbital pace, so the shape decides.
    s = classify(detected(tmp_path, meteor_profile))
    assert s["kind"] == "meteor"
    assert s["confidence"] == "medium"
    assert "elevation_deg" not in s
    assert 19.0 < s["length_deg"] < 22.0   # 500px of a 50-degree, 1200px frame
    assert any("fades in" in r for r in s["reasons"])


def test_regular_breaks_hold_the_verdict_at_low_unless_speed_settles_it():
    base = {"start": [0, 0], "end": [100, 0], "flatness": 0.5, "taper_start": 0.3,
            "taper_end": 0.02, "dashed": False, "periodic_breaks": 2}
    s = streaks.classify(dict(base), None, WIDTH, {}, [], None)
    assert s["kind"] == "meteor" and s["confidence"] == "low"
    assert any("regular intervals" in r for r in s["reasons"])
    # ...but 20 degrees in two seconds is a meteor whatever the frames did.
    wcs = synth.make_wcs(95.0, 40.0, 50.0, WIDTH, HEIGHT)
    fast = dict(base, start=[300.0, 200.0], end=[700.0, 500.0])
    s = streaks.classify(fast, wcs, WIDTH, dict(EXIF, exposure_seconds=2.0), [], None)
    assert s["kind"] == "meteor" and s["confidence"] == "high"


def test_uniform_trail_at_satellite_pace_is_a_satellite(tmp_path):
    # 20 degrees in 60 seconds: a third of a degree a second, orbital pace.
    exif = dict(EXIF, exposure_seconds=60.0)
    s = classify(detected(tmp_path), exif=exif)
    assert s["kind"] == "satellite"
    assert s["confidence"] == "medium"
    assert "satellite" not in s


def test_too_fast_for_orbit_with_a_meteor_curve_is_a_sure_meteor(tmp_path):
    # 20 degrees in two seconds: nothing in orbit, and it tapers in.
    exif = dict(EXIF, exposure_seconds=2.0)
    s = classify(detected(tmp_path, meteor_profile), exif=exif)
    assert s["kind"] == "meteor"
    assert s["confidence"] == "high"
    assert any("no satellite covers" in r for r in s["reasons"])


def test_flat_trail_too_fast_for_orbit_is_not_called_anything(tmp_path):
    # A uniform, hard-ended trail moving faster than orbit: an aircraft,
    # or a twig the pixel tests missed. Never a meteor by elimination.
    exif = dict(EXIF, exposure_seconds=2.0)
    s = classify(detected(tmp_path), exif=exif)
    assert s["kind"] == "unknown"
    assert any("aircraft" in r for r in s["reasons"])


def test_predicted_crossing_on_the_line_names_the_satellite(tmp_path):
    (x0, y0), (x1, y1) = TRAIL
    track = {"name": "ISS (ZARYA)", "norad_id": "25544",
             "points": [[x0 - 40 + (x1 - x0 + 80) * f, y0 - 30 + (y1 - y0 + 60) * f]
                        for f in np.linspace(0, 1, 9)]}
    s = classify(detected(tmp_path), crossings=[track])
    assert s["kind"] == "satellite"
    assert s["confidence"] == "high"
    assert s["satellite"]["name"] == "ISS (ZARYA)"


def test_crossing_elsewhere_does_not_claim_the_streak(tmp_path):
    track = {"name": "STARLINK-1", "norad_id": "1",
             "points": [[100.0, 800.0], [1100.0, 850.0]]}
    s = classify(detected(tmp_path), crossings=[track])
    assert "satellite" not in s
    assert s["confidence"] != "high"


def test_no_wcs_still_reports_shape():
    streak = {"start": [0, 0], "end": [100, 0], "flatness": 0.5,
              "taper_start": 0.3, "taper_end": 0.02, "dashed": False}
    s = streaks.classify(streak, None, WIDTH, {}, [], None)
    assert s["kind"] == "meteor"
    assert "length_deg" not in s


# --- showers ------------------------------------------------------------------

PERSEID_NIGHT = datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc)


def test_streak_pointing_away_from_perseid_radiant_is_a_perseid():
    # Radiant at about (48, 58) on the peak; a streak further along the
    # same great circle, moving away from it.
    start, end = (70.0, 50.0), (80.0, 45.0)
    match = streaks.match_shower(start, end, PERSEID_NIGHT)
    assert match and match["name"] == "Perseids"
    assert match["offset_deg"] < streaks.SHOWER_MAX_OFFSET_DEG


def test_same_streak_reversed_is_not_a_perseid():
    match = streaks.match_shower((80.0, 45.0), (70.0, 50.0), PERSEID_NIGHT)
    assert match is None or match["name"] != "Perseids"


def test_streak_off_the_radiant_circle_is_sporadic():
    assert streaks.match_shower((70.0, 50.0), (70.0, 30.0), PERSEID_NIGHT) is None


def test_shower_outside_its_dates_is_not_matched():
    june = datetime(2026, 6, 12, 23, 0, tzinfo=timezone.utc)
    assert streaks.match_shower((70.0, 50.0), (80.0, 45.0), june) is None


def test_quadrantid_window_straddles_new_year():
    dec30 = datetime(2025, 12, 30, 5, 0, tzinfo=timezone.utc)
    jan4 = datetime(2026, 1, 4, 5, 0, tzinfo=timezone.utc)
    jan20 = datetime(2026, 1, 20, 5, 0, tzinfo=timezone.utc)
    entry = next(e for e in streaks.SHOWERS if e[0] == "Quadrantids")
    assert streaks._radiant_on(dec30, entry) is not None
    assert streaks._radiant_on(jan4, entry) is not None
    assert streaks._radiant_on(jan20, entry) is None


def test_satellite_rate_bound_falls_toward_the_horizon():
    overhead = streaks._sat_max_rate(90.0)
    low = streaks._sat_max_rate(15.0)
    assert 1.0 < overhead < 1.4
    assert 0.3 < low < 0.5
