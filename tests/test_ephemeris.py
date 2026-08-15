"""Ephemeris layer: EXIF time resolution (pure logic) plus position tests
against DE421. The data-backed tests anchor to the 2024-04-08 total solar
eclipse — Moon and Sun coincident to within the lunar radius — so they check
the whole chain (units, frames, topocentric parallax) against reality rather
than against skyfield itself."""

import math
import os
from datetime import datetime, timedelta, timezone

import pytest
from astropy.io import fits

from app import ephemeris
from tests import synth

needs_de421 = pytest.mark.skipif(
    not os.path.exists(os.path.join(ephemeris.CATALOG_DIR, ephemeris.EPHEMERIS_FILE)),
    reason="de421.bsp not fetched (scripts/fetch-catalog.sh)",
)


def _sep(ra1, dec1, ra2, dec2):
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(dec1) * math.sin(dec2)
         + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# ---- resolve_utc: pure logic, no data files ----

def test_resolve_utc_prefers_exif_offset():
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2021:08:15 23:30:00",
        "offset_time_original": "+02:00",
        # GPS would give a very different answer; must be ignored
        "lat": 36.2, "lon": -90.0,
    })
    assert source == "exif_offset"
    assert when == datetime(2021, 8, 15, 21, 30, tzinfo=timezone.utc)


def test_resolve_utc_negative_offset():
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2024:04:08 13:17:00",
        "offset_time_original": "-05:00",
    })
    assert source == "exif_offset"
    assert when == datetime(2024, 4, 8, 18, 17, tzinfo=timezone.utc)


def test_resolve_utc_gps_timezone_is_dst_aware():
    # Nashville in July is CDT (UTC-5); the crude lon/15 guess says UTC-6.
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2025:07:04 22:00:00",
        "lat": 36.16, "lon": -86.78,
    })
    assert source == "gps_timezone"
    assert when == datetime(2025, 7, 5, 3, 0, tzinfo=timezone.utc)


def test_resolve_utc_gps_timezone_winter():
    # Same spot in January is CST (UTC-6).
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2025:01:04 22:00:00",
        "lat": 36.16, "lon": -86.78,
    })
    assert source == "gps_timezone"
    assert when == datetime(2025, 1, 5, 4, 0, tzinfo=timezone.utc)


def test_resolve_utc_gps_longitude_fallback(monkeypatch):
    # Zone lookup failing (or unknown zone) falls back to the crude guess.
    monkeypatch.setattr(ephemeris, "_zone_from_gps", lambda lat, lon: None)
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2024:01:01 00:00:00",
        "lat": 36.2, "lon": -90.0,  # ~UTC-6
    })
    assert source == "gps_longitude"
    assert when == datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)


def test_resolve_utc_longitude_only_skips_zone_lookup():
    # No latitude: the polygon lookup can't run, crude guess still can.
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2024:01:01 00:00:00",
        "lon": -90.0,
    })
    assert source == "gps_longitude"
    assert when == datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)


def test_resolve_utc_assumes_utc_without_hints():
    when, source = ephemeris.resolve_utc({"datetime_original": "2024:01:01 03:00:00"})
    assert source == "assumed_utc"
    assert when == datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)


def test_resolve_utc_handles_missing_or_garbage():
    assert ephemeris.resolve_utc({}) == (None, None)
    assert ephemeris.resolve_utc({"datetime_original": "not a date"}) == (None, None)


# ---- position/phase tests against DE421 ----

# Greatest eclipse of 2024-04-08, near Nazas, Mexico.
ECLIPSE_UTC = datetime(2024, 4, 8, 18, 17, 16, tzinfo=timezone.utc)
ECLIPSE_LAT, ECLIPSE_LON = 25.3, -104.1


@needs_de421
def test_moon_covers_sun_at_total_eclipse():
    bodies = {b["name"]: b for b in
              ephemeris.compute_bodies(ECLIPSE_UTC, ECLIPSE_LAT, ECLIPSE_LON)}
    eph = ephemeris.load_ephemeris()
    t = ephemeris._timescale().from_datetime(ECLIPSE_UTC)
    from skyfield.api import wgs84
    observer = eph["earth"] + wgs84.latlon(ECLIPSE_LAT, ECLIPSE_LON)
    ra, dec, _ = observer.at(t).observe(eph["sun"]).apparent().radec()
    moon = bodies["Moon"]
    assert _sep(moon["ra"], moon["dec"],
                float(ra.hours) * 15.0, float(dec.degrees)) < 0.3


@needs_de421
def test_moon_phase_new_at_eclipse_full_two_weeks_later():
    new = ephemeris.compute_bodies(ECLIPSE_UTC, ECLIPSE_LAT, ECLIPSE_LON)
    assert {b["name"] for b in new} == {"Moon", "Mercury", "Venus", "Mars",
                                        "Jupiter", "Saturn"}
    assert next(b for b in new if b["name"] == "Moon")["phase"] < 0.02

    full_utc = datetime(2024, 4, 23, 23, 49, tzinfo=timezone.utc)
    full = ephemeris.compute_bodies(full_utc)
    assert next(b for b in full if b["name"] == "Moon")["phase"] > 0.98


@needs_de421
def test_topocentric_parallax_moves_the_moon():
    geo = next(b for b in ephemeris.compute_bodies(ECLIPSE_UTC) if b["name"] == "Moon")
    topo = next(b for b in ephemeris.compute_bodies(ECLIPSE_UTC, ECLIPSE_LAT, ECLIPSE_LON)
                if b["name"] == "Moon")
    sep = _sep(geo["ra"], geo["dec"], topo["ra"], topo["dec"])
    assert 0.2 < sep < 1.5  # lunar parallax is most of a degree


@needs_de421
def test_annotate_bodies_projects_into_frame(tmp_path):
    width, height = 1200, 900
    jupiter = next(b for b in ephemeris.compute_bodies(ECLIPSE_UTC, ECLIPSE_LAT, ECLIPSE_LON)
                   if b["name"] == "Jupiter")
    wcs = synth.make_wcs(ra=jupiter["ra"], dec=jupiter["dec"], fov_deg=40.0,
                         width=width, height=height)
    wcs_path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(wcs_path)

    exif_info = {
        "datetime_original": "2024:04:08 12:17:16",
        "offset_time_original": "-06:00",
        "lat": ECLIPSE_LAT, "lon": ECLIPSE_LON,
    }
    labels, meta = ephemeris.annotate_bodies(str(wcs_path), width, height, exif_info)

    assert meta["time_source"] == "exif_offset"
    assert meta["time_utc"].startswith("2024-04-08T18:17:16")
    named = {l["name"]: l for l in labels}
    assert "Jupiter" in named
    assert named["Jupiter"]["kind"] == "planet"
    assert named["Jupiter"]["x"] == pytest.approx(width / 2, abs=5)
    assert named["Jupiter"]["y"] == pytest.approx(height / 2, abs=5)
    for l in labels:
        assert 0 <= l["x"] < width and 0 <= l["y"] < height


# ---- no-solve fallback (#7) ----

# The real case that motivated the issue: Pixel 9a dusk shots from Nashville,
# 2026-08-11 20:10 CDT. Venus was the only planet up (az ~254 true, alt ~16,
# mag -4.4); the phone recorded a magnetic heading of ~170 but no GPS fix in
# some frames. Local declination is about -4.2 (WMM).
VENUS_EXIF = {
    "datetime_original": "2026:08:11 20:10:00",
    "offset_time_original": "-05:00",
    "lat": 36.1, "lon": -86.8,
    "heading": 170.0, "heading_ref": "M",
}


def test_compass_names():
    assert ephemeris._compass(0) == "N"
    assert ephemeris._compass(254) == "WSW"
    assert ephemeris._compass(359) == "N"


def test_declination_matches_wmm_for_nashville():
    from datetime import datetime, timezone
    d = ephemeris._declination(36.1, -86.8, datetime(2026, 8, 12, tzinfo=timezone.utc))
    assert d == pytest.approx(-4.2, abs=0.5)


@needs_de421
def test_fallback_guess_identifies_venus_at_dusk():
    guess = ephemeris.fallback_guess(VENUS_EXIF)
    assert guess["location_source"] == "gps"
    assert -15 <= guess["sun_alt_deg"] <= 0  # dusk, not night
    # magnetic 170 corrected by ~-4.2 declination
    assert guess["heading_true"] == pytest.approx(165.8, abs=1.0)

    venus = next(c for c in guess["candidates"] if c["name"] == "Venus")
    assert venus["az_deg"] == pytest.approx(254, abs=4)
    assert venus["alt_deg"] == pytest.approx(16, abs=4)
    assert venus["mag"] < -3.5
    assert venus["direction"] == "WSW"
    # Venus sat well to the right of where the phone pointed
    assert venus["offset_deg"] == pytest.approx(88, abs=6)

    # brightest first
    mags = [c["mag"] if c["mag"] is not None else -99 for c in guess["candidates"]]
    assert mags == sorted(mags)


@needs_de421
def test_fallback_guess_without_gps_uses_timezone_and_hedges():
    guess = ephemeris.fallback_guess({
        "datetime_original": "2026:08:11 20:10:00",
        "offset_time_original": "-05:00",
    })
    assert guess["location_source"] == "timezone_guess"
    assert "heading_true" not in guess  # no heading recorded
    for c in guess["candidates"]:
        assert "offset_deg" not in c


def test_fallback_guess_needs_time_and_some_location_hint():
    assert ephemeris.fallback_guess({}) is None
    # timestamp but no GPS and no zone: alt/az would be fiction
    assert ephemeris.fallback_guess(
        {"datetime_original": "2026:08:11 20:10:00"}) is None


@needs_de421
def test_annotate_bodies_without_timestamp_is_quiet(tmp_path):
    wcs = synth.make_wcs(ra=95.0, dec=-10.0, fov_deg=40.0, width=100, height=100)
    wcs_path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(wcs_path)
    labels, meta = ephemeris.annotate_bodies(str(wcs_path), 100, 100, {})
    assert labels == []
    assert meta == {"time_utc": None, "time_source": None}


# ---- location band and horizon cut for the no-solve guess (#79, #80) ----

# The dusk walk that exposed both bugs: a 7%-lit crescent Moon photographed
# over a rooftop, with no GPS in the upload. The solve failed (twilight, four
# star-like sources) and the guess named nothing at all.
DUSK_WALK = {"datetime_original": "2026:08:14 20:28:00",
             "offset_time_original": "-05:00"}
NASHVILLE = {"lat": 36.16, "lon": -86.78}


def test_guess_longitudes_offers_both_standard_and_daylight():
    # -05:00 is either EST (meridian -75) or CDT (standard -06:00, so -90).
    assert ephemeris.guess_longitudes(timedelta(hours=-5)) == [-90.0, -75.0]


def test_guess_longitudes_wrap_past_the_date_line():
    # UTC+14 exists (the Line Islands, around 157W). Both hypotheses land past
    # 180 degrees east and have to wrap; clamping would put them on the wrong
    # side of the planet.
    lons = ephemeris.guess_longitudes(timedelta(hours=14))
    assert lons == [-165.0, -150.0]
    assert lons[0] < -157.0 < lons[1], "should bracket Kiritimati"


def test_moon_is_named_without_gps():
    guess = ephemeris.fallback_guess(dict(DUSK_WALK))
    assert guess["location_source"] == "timezone_guess"
    names = [c["name"] for c in guess["candidates"]]
    assert "Moon" in names, guess
    moon = next(c for c in guess["candidates"] if c["name"] == "Moon")
    # Thin crescent, and low enough that the old 3-degree floor hid it.
    assert moon["phase"] == pytest.approx(0.07, abs=0.02)
    assert moon["direction"] in ("W", "WSW", "WNW")
    # The timezone spans a zone; the altitude spread says so out loud.
    low, high = moon["alt_range_deg"]
    assert low < 0 < high, moon


def test_moon_is_named_with_gps_at_the_real_location():
    # +2.7 degrees at the capture location: visible in the frame, and under
    # the floor this used to apply.
    guess = ephemeris.fallback_guess(dict(DUSK_WALK, **NASHVILLE))
    assert guess["location_source"] == "gps"
    moon = next((c for c in guess["candidates"] if c["name"] == "Moon"), None)
    assert moon is not None, guess
    assert 0 <= moon["alt_deg"] <= 5
    # A known location carries no band, so no range to report.
    assert "alt_range_deg" not in moon


def test_bodies_below_the_horizon_everywhere_in_the_band_stay_out():
    # Four hours later the crescent has followed the Sun down, and it is gone
    # from both ends of the timezone — so widening the search must not start
    # inventing things that genuinely set.
    guess = ephemeris.fallback_guess({"datetime_original": "2026:08:15 00:28:00",
                                      "offset_time_original": "-05:00"})
    assert all(c["name"] != "Moon" for c in guess["candidates"]), guess


def test_gps_path_is_unchanged_by_the_band_logic():
    guess = ephemeris.fallback_guess(dict(DUSK_WALK, **NASHVILLE))
    for cand in guess["candidates"]:
        assert "alt_range_deg" not in cand
        assert cand["alt_deg"] >= ephemeris.FALLBACK_MIN_ALT_DEG
