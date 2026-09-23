"""What the night was like (#121) and how deep it reached (#122). The
sentence builder is pinned branch by branch on hand-built facts, the
observer grid on trig anyone can check, and the ephemeris-backed pass on
three real uploads' EXIF whose answers are known from the sky."""

import os

import numpy as np
import pytest
from astropy.io import fits

from app import ephemeris, night
from tests import synth

needs_de421 = pytest.mark.skipif(
    not os.path.exists(os.path.join(ephemeris.CATALOG_DIR, ephemeris.EPHEMERIS_FILE)),
    reason="de421.bsp not fetched (scripts/fetch-catalog.sh)",
)

# Three real uploads from the week of 2026-09-14, EXIF only.
ALPHA_CEN_DUSK = {  # southern sky at dusk, clock offset but no GPS
    "datetime_original": "2026:09:12 18:18:47",
    "offset_time_original": "-03:00", "exposure_seconds": 1.0,
}
ALPHA_CEN_CENTRE = (219.9, -60.8)
BALTIC_NIGHT = {  # Poland's coast, full dark, GPS
    "datetime_original": "2026:09:06 22:13:11",
    "offset_time_original": "+02:00", "exposure_seconds": 2.0,
    "lat": 54.8, "lon": 17.9,
}
VEGA_CENTRE = (279.2, 38.8)
NASHVILLE_TWILIGHT = {  # a July evening, GPS: the Sun still setting
    "datetime_original": "2025:07:04 21:00:00",
    "offset_time_original": "-05:00", "exposure_seconds": 10.0,
    "lat": 36.16, "lon": -86.78,
}
ARCTURUS_CENTRE = (213.9, 19.2)


def _wcs_file(tmp_path, centre):
    wcs = synth.make_wcs(ra=centre[0], dec=centre[1], fov_deg=55.0,
                         width=3024, height=4032)
    path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(path)
    return str(path)


# ---- the observer grid: pure trig ----

def test_gps_settles_the_observer():
    when, _ = ephemeris.resolve_utc(BALTIC_NIGHT)
    assert night.observers(BALTIC_NIGHT, VEGA_CENTRE, when) == ([(54.8, 17.9)], "gps")


def test_no_offset_and_no_gps_means_nowhere_to_stand():
    exif = {"datetime_original": "2026:09:12 18:18:47"}
    when, _ = ephemeris.resolve_utc(exif)
    assert night.observers(exif, ALPHA_CEN_CENTRE, when) == ([], None)


def test_offset_only_grid_keeps_positions_that_could_see_the_field():
    # Alpha Centauri (Dec -61) never clears the horizon north of 29 N, so
    # the northern half of the grid is thrown out; the two longitude
    # hypotheses of a -03:00 clock (#79) are both kept.
    when, _ = ephemeris.resolve_utc(ALPHA_CEN_DUSK)
    positions, source = night.observers(ALPHA_CEN_DUSK, ALPHA_CEN_CENTRE, when)
    assert source == "timezone_guess"
    assert positions
    assert max(lat for lat, _ in positions) <= 25.0
    assert min(lat for lat, _ in positions) == -55.0
    assert {lon for _, lon in positions} == {-60.0, -45.0}


def test_dark_crossing_reads_dusk_and_dawn_off_the_track():
    falling = np.linspace(10.0, -30.0, 401)  # -10 at mid, -18 eighty steps on
    minutes = 80 * night.STEP_MINUTES
    assert night._dark_crossing(falling, 200) == (True, minutes)
    assert night._dark_crossing(falling[::-1], 200) == (False, minutes)
    # never reaching full dark: no number
    assert night._dark_crossing(np.linspace(-5.0, -15.0, 401), 200) == (True, None)


def test_minutes_are_quoted_only_when_the_grid_agrees():
    assert night._round_minutes(58, 62) == 60
    assert night._round_minutes(2, 3) == 5   # never "about 0 minutes"
    assert night._round_minutes(40, 90) is None


# ---- the sentences: every branch on hand-built facts ----

def _sun(**kw):
    base = {"alt_range_deg": [-10, -10], "darkness": "twilight", "dusk": True,
            "minutes_to_dark": None, "minutes_since_dark": None,
            "never_dark": False}
    return {**base, **kw}


def _moon(name, illuminated, up=None, in_frame=False, near_frame=False):
    return {"illuminated": illuminated, "waxing": name.startswith("wax") or name == "first quarter",
            "name": name, "up": up, "in_frame": in_frame, "near_frame": near_frame}


def test_twilight_lines():
    assert night.describe({"sun": _sun(minutes_to_dark=40)}) == [
        "Taken in twilight, about 40 minutes before the sky was fully dark."]
    assert night.describe({"sun": _sun()}) == [
        "Taken in twilight, before the sky was fully dark."]
    assert night.describe({"sun": _sun(never_dark=True)}, month="June") == [
        "Taken in twilight; at this latitude in June the sky never gets fully dark."]
    assert night.describe({"sun": _sun(dusk=False, minutes_since_dark=25)}) == [
        "Taken in morning twilight, about 25 minutes after full dark ended."]
    assert night.describe({"sun": _sun(dusk=None)}) == [
        "Taken in twilight rather than full dark."]
    # full dark on its own is not worth a sentence; it rides with the Moon
    assert night.describe({"sun": _sun(darkness="night")}) == []


def test_moon_lines():
    d = night.describe
    assert d({"sun": _sun(darkness="night"), "moon": _moon("new", 0.01)}) == [
        "The sky was fully dark and the Moon was new: no moonlight, no twilight, just stars."]
    assert d({"moon": _moon("new", 0.02)}) == [
        "The Moon was new, so it added no light to the sky."]
    assert d({"moon": _moon("waxing gibbous", 0.81, up=True)}) == [
        "The Moon was a waxing gibbous, 81% lit and up, which brightens the whole sky and hides the fainter stars."]
    assert d({"moon": _moon("waning crescent", 0.22, up=True)}) == [
        "The Moon was a waning crescent, 22% lit and up, adding only a little light."]
    assert d({"moon": _moon("waning crescent", 0.22, up=False)}) == [
        "The Moon was a waning crescent, 22% lit and below the horizon."]
    assert d({"moon": _moon("full", 0.99, up=False)}) == [
        "The Moon was full but below the horizon, so it took nothing from the sky."]
    assert d({"moon": _moon("first quarter", 0.52, in_frame=True)}) == [
        "The Moon in the frame was at first quarter, 52% lit, bright enough to wash out the fainter stars."]
    assert d({"moon": _moon("waxing crescent", 0.30, near_frame=True)}) == [
        "The Moon just outside the frame was a waxing crescent, 30% lit."]
    # Altitude unknown: the line has to close the question itself, or the
    # narrator closes it — a 62%-lit Moon with no location became "the
    # waxing gibbous Moon absent" in prod.
    assert d({"moon": _moon("waning gibbous", 0.70)}) == [
        "The Moon was a waning gibbous, 70% lit, though there is no telling "
        "from this photo whether it had risen."]
    # Bright and dim take the same wording: brightness says nothing about
    # whether it was above the horizon.
    assert d({"moon": _moon("waxing gibbous", 0.88)}) == [
        "The Moon was a waxing gibbous, 88% lit, though there is no telling "
        "from this photo whether it had risen."]


def test_moon_names():
    assert night._moon_name(0.01, True) == "new"
    assert night._moon_name(0.20, True) == "waxing crescent"
    assert night._moon_name(0.20, False) == "waning crescent"
    assert night._moon_name(0.50, True) == "first quarter"
    assert night._moon_name(0.50, False) == "last quarter"
    assert night._moon_name(0.80, True) == "waxing gibbous"
    assert night._moon_name(0.98, False) == "full"


def test_depth_lines():
    d = night.describe
    assert d({"depth": {"limiting_mag": 5.9, "exposure_seconds": 10.0}}) == [
        "Stars down to magnitude 5.9 show in this photo, right around the "
        "naked-eye limit, which takes a dark sky, from a 10-second exposure."]
    assert d({"depth": {"limiting_mag": 3.0, "exposure_seconds": 0.25}}) == [
        "Stars down to magnitude 3.0 show in this photo, the bright ones "
        "that show even from a city, from a 1/4-second exposure."]
    assert d({"depth": {"limiting_mag": 4.2, "exposure_seconds": 2.5}}) == [
        "Stars down to magnitude 4.2 show in this photo, about what the eye "
        "picks out from the suburbs, from a 2.5-second exposure."]
    assert d({"depth": {"limiting_mag": 7.0, "exposure_seconds": None}}) == [
        "Stars down to magnitude 7.0 show in this photo, fainter than the "
        "unaided eye can see."]
    assert d({"depth": {"limiting_mag": None}}) == []
    # the catalog ran out before the photo did: a floor, said as one
    assert d({"depth": {"limiting_mag": 8.0, "catalog_limited": True,
                        "exposure_seconds": 16.0}}) == [
        "Stars to at least magnitude 8.0 show in this photo, fainter than the "
        "unaided eye can see, from a 16-second exposure."]


def test_lines_read_sun_then_moon_then_depth():
    lines = night.describe({
        "sun": _sun(minutes_to_dark=40),
        "moon": _moon("waning crescent", 0.22, up=False),
        "depth": {"limiting_mag": 4.2, "exposure_seconds": 1.0},
    })
    assert [l.split()[0] for l in lines] == ["Taken", "The", "Stars"]
    assert len(lines) == 3


def test_no_timestamp_still_gets_the_depth_line():
    out = night.annotate({"exposure_seconds": 3.0}, None, [], [],
                         {"verified": True, "depth": {"limiting_mag": 5.1}})
    assert out["sun"] is None and out["moon"] is None
    assert out["depth"] == {"limiting_mag": 5.1, "catalog_limited": False,
                            "exposure_seconds": 3.0}
    assert out["lines"] == ["Stars down to magnitude 5.1 show in this photo, "
                            "right around the naked-eye limit, which takes a "
                            "dark sky, from a 3-second exposure."]


# ---- against the sky ----

@needs_de421
def test_dusk_without_gps_resolves_to_twilight(tmp_path):
    # 18:18 at UTC-3 on 12 September, Alpha Centauri in the frame. The
    # daylight-saving longitude hypothesis puts the Sun above the horizon
    # and is discarded on the strength of the stars in the photo; every
    # surviving position agrees it was twilight, but they disagree by more
    # than 20 minutes on how long until full dark, so no number is quoted.
    out = night.annotate(ALPHA_CEN_DUSK, _wcs_file(tmp_path, ALPHA_CEN_CENTRE),
                         [], [], {"verified": True})
    assert out["location_source"] == "timezone_guess"
    assert out["sun"]["darkness"] == "twilight"
    assert out["sun"]["dusk"] is True
    assert -18 < out["sun"]["alt_range_deg"][0] <= out["sun"]["alt_range_deg"][1] < 0
    assert out["lines"][0].startswith("Taken in twilight")
    # a day and a half past new: barely lit, waxing
    assert out["moon"]["illuminated"] < 0.10
    assert out["moon"]["waxing"] is True


@needs_de421
def test_full_dark_with_gps(tmp_path):
    out = night.annotate(BALTIC_NIGHT, _wcs_file(tmp_path, VEGA_CENTRE),
                         [], [], {"verified": True})
    assert out["location_source"] == "gps"
    assert out["sun"]["darkness"] == "night"
    assert out["sun"]["alt_range_deg"][1] <= -18
    # a waning crescent five days before new, not yet risen at 22:13
    assert out["moon"]["name"] == "waning crescent"
    assert out["moon"]["up"] is False
    assert out["lines"] == [
        f"The Moon was a waning crescent, {round(out['moon']['illuminated'] * 100)}% "
        "lit and below the horizon."]


@needs_de421
def test_twilight_with_gps_quotes_the_minutes(tmp_path):
    # Nashville, 4 July 2025, 21:00 CDT: sunset was 20:08, astronomical
    # dusk about 21:58, and a two-day-old first-quarter Moon was up.
    out = night.annotate(NASHVILLE_TWILIGHT, _wcs_file(tmp_path, ARCTURUS_CENTRE),
                         [], [], {"verified": True})
    assert out["sun"]["darkness"] == "twilight" and out["sun"]["dusk"] is True
    assert 45 <= out["sun"]["minutes_to_dark"] <= 75
    assert out["moon"]["name"] == "waxing gibbous" and out["moon"]["up"] is True
    assert out["lines"][0] == (f"Taken in twilight, about {out['sun']['minutes_to_dark']} "
                               "minutes before the sky was fully dark.")
    assert out["lines"][1].endswith("and up, which brightens the whole sky and hides the fainter stars.")
