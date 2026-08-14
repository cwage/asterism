"""Satellite crossings (#11): TLE caching, propagation, and projection.
Fully offline — Space-Track fetches are stubbed and the element set is a
fixture, so nothing here touches the network."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from astropy.io import fits

from app import db, ephemeris, satellites
from tests import synth

# A realistic ISS element set. Epoch is 2026 day 224.5 (Aug 12), so the
# propagation below stays close to epoch where SGP4 is accurate.
TLE_1 = "1 25544U 98067A   26224.50000000  .00016717  00000-0  30074-3 0  9993"
TLE_2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.4981531012345"

LAT, LON = 38.0, -85.0
WIDTH, HEIGHT = 1600, 1200
WHEN = datetime(2026, 8, 12, 4, 0, 0, tzinfo=timezone.utc)
EXPOSURE = 16.0

EXIF = {"datetime_original": "2026:08:12 04:00:00",
        "offset_time_original": "Z", "exposure_seconds": EXPOSURE,
        "lat": LAT, "lon": LON, "width": WIDTH, "height": HEIGHT}


def _row(norad="25544", name="ISS (ZARYA)", epoch="2026-08-12T12:00:00"):
    return {"NORAD_CAT_ID": norad, "OBJECT_NAME": name, "EPOCH": epoch,
            "TLE_LINE1": TLE_1, "TLE_LINE2": TLE_2}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Per-test TLE cache dir, credentials, and a clear satellite cache."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPACETRACK_USER", "user@example.com")
    monkeypatch.setenv("SPACETRACK_PASS", "hunter2")
    satellites._sat_cache.clear()
    yield
    satellites._sat_cache.clear()


@pytest.fixture()
def iss():
    from skyfield.api import EarthSatellite
    return EarthSatellite(TLE_1, TLE_2, "ISS (ZARYA)", ephemeris._timescale())


def iss_radec(iss, when):
    """Where the fixture satellite actually is, for aiming a test frame."""
    from skyfield.api import wgs84
    t = ephemeris._timescale().from_datetime(when)
    ra, dec, _ = (iss - wgs84.latlon(LAT, LON)).at(t).radec()
    return ra.hours * 15.0, dec.degrees


def test_crossing_found_when_frame_points_at_the_satellite(iss):
    ra, dec = iss_radec(iss, WHEN + timedelta(seconds=EXPOSURE / 2))
    wcs = synth.make_wcs(ra, dec, 50.0, WIDTH, HEIGHT)

    out = satellites.compute_crossings(wcs, WIDTH, HEIGHT, WHEN, EXPOSURE,
                                       LAT, LON, [("Iss (Zarya)", "25544", iss)])
    assert len(out) == 1
    crossing = out[0]
    assert crossing["name"] == "Iss (Zarya)"
    assert crossing["norad_id"] == "25544"
    # a track, not a dot, and every point inside the frame
    assert len(crossing["points"]) >= 2
    assert all(0 <= x < WIDTH and 0 <= y < HEIGHT
               for x, y in crossing["points"])
    # in frame for the whole exposure, since we aimed at its midpoint
    assert crossing["t_enter_s"] == 0.0
    assert crossing["t_exit_s"] == EXPOSURE


def test_no_crossing_when_frame_points_elsewhere(iss):
    # The antipode: gnomonic projection folds the opposite hemisphere back
    # onto the frame, so a pixel-only test would "find" the satellite here.
    ra, dec = iss_radec(iss, WHEN)
    wcs = synth.make_wcs((ra + 180.0) % 360.0, -dec, 50.0, WIDTH, HEIGHT)
    out = satellites.compute_crossings(wcs, WIDTH, HEIGHT, WHEN, EXPOSURE,
                                       LAT, LON, [("Iss (Zarya)", "25544", iss)])
    assert out == []


def test_crossings_are_capped_and_longest_first(iss, monkeypatch):
    monkeypatch.setattr(satellites, "MAX_CROSSINGS", 3)
    ra, dec = iss_radec(iss, WHEN + timedelta(seconds=EXPOSURE / 2))
    wcs = synth.make_wcs(ra, dec, 50.0, WIDTH, HEIGHT)
    sats = [(f"Sat {i}", str(i), iss) for i in range(6)]
    out = satellites.compute_crossings(wcs, WIDTH, HEIGHT, WHEN, EXPOSURE,
                                       LAT, LON, sats)
    assert len(out) == 3
    lengths = [len(c["points"]) for c in out]
    assert lengths == sorted(lengths, reverse=True)


def test_annotate_projects_through_the_solved_wcs(tmp_path, iss):
    ra, dec = iss_radec(iss, WHEN + timedelta(seconds=EXPOSURE / 2))
    wcs_path = tmp_path / "solve.wcs"
    fits.PrimaryHDU(header=synth.make_wcs(ra, dec, 50.0, WIDTH, HEIGHT)
                    .to_header()).writeto(wcs_path)

    calls = []
    def fake_fetch(url, user, password):
        calls.append(url)
        return [_row()]

    out = satellites.annotate(str(wcs_path), WIDTH, HEIGHT, EXIF,
                              fetch=fake_fetch)
    assert len(calls) == 1
    assert out["objects_checked"] == 1
    assert out["exposure_seconds"] == EXPOSURE
    assert [c["name"] for c in out["crossings"]] == ["Iss (Zarya)"]


def test_annotate_skips_without_timestamp():
    assert satellites.annotate("/x.wcs", WIDTH, HEIGHT, {}) == \
        {"skipped": "no_timestamp"}


def test_annotate_skips_without_gps():
    exif = dict(EXIF, lat=None, lon=None)
    # Low-orbit parallax makes positions meaningless without an observer.
    assert satellites.annotate("/x.wcs", WIDTH, HEIGHT, exif) == \
        {"skipped": "no_gps"}


def test_annotate_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("SPACETRACK_USER", raising=False)
    monkeypatch.delenv("SPACETRACK_PASS", raising=False)
    assert satellites.annotate("/x.wcs", WIDTH, HEIGHT, EXIF) == \
        {"skipped": "no_credentials"}


def test_tle_records_cache_avoids_a_second_fetch(tmp_path):
    calls = []
    def fake_fetch(url, user, password):
        calls.append(url)
        return [_row()]

    first, path, _ = satellites._tle_records(WHEN, fetch=fake_fetch)
    second, path2, _ = satellites._tle_records(WHEN, fetch=fake_fetch)
    assert len(calls) == 1, "the second solve must reuse the cached TLE set"
    assert first == second and path == path2
    assert json.loads(open(path).read()) == [_row()]


def test_tle_records_keeps_the_elset_nearest_the_exposure():
    rows = [
        _row(epoch="2026-08-10T00:00:00"),  # two days stale
        _row(epoch="2026-08-12T03:00:00"),  # an hour before the shot
        _row(epoch="2026-08-14T00:00:00"),
        _row(norad="99999", name="OTHER", epoch="2026-08-12T05:00:00"),
    ]
    records, _, _ = satellites._tle_records(WHEN, fetch=lambda *a: rows)
    by_id = {r["NORAD_CAT_ID"]: r for r in records}
    assert len(records) == 2, "one element set per object"
    assert by_id["25544"]["EPOCH"] == "2026-08-12T03:00:00"


def test_cache_is_pruned_to_a_bounded_number_of_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(satellites, "TLE_CACHE_KEEP", 3)
    for day in range(6):
        when = WHEN - timedelta(days=day)
        satellites._tle_records(when, fetch=lambda *a: [_row()])
    kept = list((tmp_path / "tle").glob("*.json"))
    assert len(kept) == 3


def test_query_url_switches_to_history_for_old_photos():
    recent = datetime.now(timezone.utc) - timedelta(hours=6)
    old = datetime.now(timezone.utc) - timedelta(days=45)
    assert satellites._query_url(recent)[1] == "gp"
    url, source = satellites._query_url(old)
    assert source == "gp_history"
    assert f"{old.date() - timedelta(days=1)}--" in url


def test_clean_name_tidies_catalog_names():
    assert satellites._clean_name("ISS (ZARYA)", "25544") == "Iss (Zarya)"
    assert satellites._clean_name("  STARLINK-1234  ", "1") == "Starlink-1234"
    assert satellites._clean_name(None, "12345") == "NORAD 12345"
