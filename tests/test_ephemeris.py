"""Ephemeris layer: EXIF time resolution (pure logic) plus position tests
against DE421. The data-backed tests anchor to the 2024-04-08 total solar
eclipse — Moon and Sun coincident to within the lunar radius — so they check
the whole chain (units, frames, topocentric parallax) against reality rather
than against skyfield itself."""

import math
import os
from datetime import datetime, timezone

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
        "lon": -90.0,  # would give a very different answer; must be ignored
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


def test_resolve_utc_gps_longitude_fallback():
    when, source = ephemeris.resolve_utc({
        "datetime_original": "2024:01:01 00:00:00",
        "lon": -90.0,  # ~UTC-6
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


@needs_de421
def test_annotate_bodies_without_timestamp_is_quiet(tmp_path):
    wcs = synth.make_wcs(ra=95.0, dec=-10.0, fov_deg=40.0, width=100, height=100)
    wcs_path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=wcs.to_header()).writeto(wcs_path)
    labels, meta = ephemeris.annotate_bodies(str(wcs_path), 100, 100, {})
    assert labels == []
    assert meta == {"time_utc": None, "time_source": None}
