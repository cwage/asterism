"""Satellite crossings from archived TLEs (#11): which satellites passed
through the frame while the shutter was open.

Scope (v1): just list the crossings — Space-Track TLEs propagated with
sgp4 (via skyfield) over the EXIF exposure window, projected through the
solved WCS. Streak *detection* in the pixels is its own problem and is
explicitly not attempted; the UI draws the computed track dashed, the
same visual language as an unverified label.

Needs GPS as well as a timestamp: low-orbit parallax is enormous, and
the same satellite sits tens of degrees apart for observers a few
hundred km apart. Fetches are cached per UTC date under DATA_DIR/tle, so
a night's worth of uploads costs one Space-Track query — well inside
their rate guidance (~30/min, and don't re-pull the same data).
"""

import http.cookiejar
import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import db, ephemeris

LOGIN_URL = "https://www.space-track.org/ajaxauth/login"
BASE_URL = "https://www.space-track.org/basicspacedata/query"
# The plausibly-photographable subset: low orbit (mean motion > 11 rev/day)
# and radar cross-section LARGE (>1 m^2). Smaller or higher objects are
# invisible to a phone and would only pad the list.
QUERY_FILTERS = "MEAN_MOTION/%3E11/RCS_SIZE/LARGE"
PREDICATES = "NORAD_CAT_ID,OBJECT_NAME,EPOCH,TLE_LINE1,TLE_LINE2"
FETCH_TIMEOUT_SECONDS = 60
# Recent shots use gp (the current catalog of latest element sets); older
# ones use gp_history for element sets around the shot's own date.
GP_FRESH_DAYS = 3
TLE_CACHE_KEEP = 30  # per-date files; ~1 per night of uploads

DEFAULT_EXPOSURE_SECONDS = 1.0  # no EXIF exposure: treat it as an instant
SAMPLE_HZ = 2.0                 # track samples per second of exposure
MAX_SAMPLES = 33
MAX_CROSSINGS = 12              # a Starlink train would otherwise flood the UI
# A 400km-altitude pass moves at roughly 1.1 deg/s overhead; pad the coarse
# search radius by this per second of exposure so nothing entering the
# frame mid-exposure is missed.
MAX_RATE_DEG_S = 1.5

_sat_cache = {}  # tle cache path -> [(name, norad, EarthSatellite)]


def _credentials():
    user = os.environ.get("SPACETRACK_USER")
    password = os.environ.get("SPACETRACK_PASS")
    return (user, password) if user and password else None


def _query_url(when_utc):
    """(url, source) — the live catalog for recent shots, historical
    element sets bracketing the date for older ones."""
    age = datetime.now(timezone.utc) - when_utc
    if age < timedelta(days=GP_FRESH_DAYS):
        return (f"{BASE_URL}/class/gp/{QUERY_FILTERS}"
                f"/predicates/{PREDICATES}/format/json", "gp")
    day = when_utc.date()
    lo, hi = day - timedelta(days=1), day + timedelta(days=2)
    return (f"{BASE_URL}/class/gp_history/EPOCH/{lo}--{hi}/{QUERY_FILTERS}"
            f"/predicates/{PREDICATES}/format/json", "gp_history")


def _fetch(url, user, password):
    """One login plus one query on a throwaway session. Space-Track has no
    API keys: the account password is the credential, exchanged for a
    session cookie."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    creds = urllib.parse.urlencode(
        {"identity": user, "password": password}).encode()
    opener.open(LOGIN_URL, creds, timeout=FETCH_TIMEOUT_SECONDS)
    with opener.open(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _prune_cache(cache_dir):
    """Keep the cache bounded: TLE sets are a few MB per date and nothing
    else ever deletes them (the retention sweep only knows about jobs)."""
    try:
        files = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir)
                 if f.endswith(".json")]
    except OSError:
        return
    for path in sorted(files, key=os.path.getmtime)[:-TLE_CACHE_KEEP]:
        try:
            os.unlink(path)
        except OSError:
            pass


def _tle_records(when_utc, fetch=_fetch):
    """(records, cache_path, source): one TLE row per object, the element
    set whose epoch sits nearest the exposure. records is None when the
    layer can't run, with source carrying the reason."""
    creds = _credentials()
    if not creds:
        return None, None, "no_credentials"

    url, source = _query_url(when_utc)
    cache_dir = os.path.join(db.DATA_DIR, "tle")
    cache_path = os.path.join(
        cache_dir, f"{source}-{when_utc.date().isoformat()}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            rows = json.load(f)
    else:
        rows = fetch(url, *creds)
        os.makedirs(cache_dir, exist_ok=True)
        # Atomic publish: a second worker must never read a half-written set.
        tmp_path = f"{cache_path}.tmp{os.getpid()}"
        with open(tmp_path, "w") as f:
            json.dump(rows, f)
        os.replace(tmp_path, cache_path)
        _prune_cache(cache_dir)

    # gp_history returns several element sets per object; keep the one
    # closest in time to the shot, which is the most accurate to propagate.
    best = {}
    for row in rows:
        try:
            epoch = datetime.fromisoformat(row["EPOCH"])
            if epoch.tzinfo is None:
                epoch = epoch.replace(tzinfo=timezone.utc)
            key = row["NORAD_CAT_ID"]
        except (KeyError, TypeError, ValueError):
            continue
        dist = abs((epoch - when_utc).total_seconds())
        if key not in best or dist < best[key][0]:
            best[key] = (dist, row)
    return [row for _, row in best.values()], cache_path, source


def _clean_name(name, norad):
    name = re.sub(r"\s+", " ", (name or "").strip())
    return name.title() if name else f"NORAD {norad}"


def _satellites(records, cache_path):
    """EarthSatellite objects for the records — parsed once per TLE set
    rather than once per solve."""
    if cache_path in _sat_cache:
        return _sat_cache[cache_path]
    from skyfield.api import EarthSatellite

    ts = ephemeris._timescale()
    sats = []
    for row in records:
        try:
            sat = EarthSatellite(row["TLE_LINE1"], row["TLE_LINE2"],
                                 row.get("OBJECT_NAME"), ts)
        except (KeyError, TypeError, ValueError):
            continue
        sats.append((_clean_name(row.get("OBJECT_NAME"),
                                 row.get("NORAD_CAT_ID")),
                     row.get("NORAD_CAT_ID"), sat))
    _sat_cache.clear()  # dates roll over; never hold more than one set
    _sat_cache[cache_path] = sats
    return sats


def _sep_deg(ra1, dec1, ra2, dec2):
    """Angular separation between two sky positions, degrees in and out."""
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    cos_sep = (math.sin(d1) * math.sin(d2)
               + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def _radec_deg(sat, observer, times):
    ra, dec, _ = (sat - observer).at(times).radec()
    return ra.hours * 15.0, dec.degrees


def compute_crossings(wcs, width, height, when_utc, exposure_s, lat, lon,
                      sats):
    """Crossings for satellites whose track enters the frame during
    [when_utc, when_utc + exposure_s]. Each is a pixel polyline plus the
    seconds into the exposure when it entered and left."""
    import numpy as np
    from skyfield.api import wgs84

    ts = ephemeris._timescale()
    observer = wgs84.latlon(lat, lon)

    center = wcs.all_pix2world([[width / 2.0, height / 2.0]], 0)[0]
    corner = wcs.all_pix2world([[0.0, 0.0]], 0)[0]
    radius = _sep_deg(center[0], center[1], corner[0], corner[1])

    # Coarse pass: one propagation per object at the exposure midpoint.
    # Gnomonic projection folds the opposite hemisphere back onto the
    # frame, so candidates must be chosen by true angular distance before
    # any pixel math happens.
    mid = ts.from_datetime(when_utc + timedelta(seconds=exposure_s / 2.0))
    coarse_radius = radius + exposure_s * MAX_RATE_DEG_S + 1.0
    candidates = []
    for name, norad, sat in sats:
        try:
            ra_deg, dec_deg = _radec_deg(sat, observer, mid)
        except Exception:
            continue  # decayed or otherwise unpropagatable element set
        if not (math.isfinite(ra_deg) and math.isfinite(dec_deg)):
            continue
        if _sep_deg(ra_deg, dec_deg, center[0], center[1]) <= coarse_radius:
            candidates.append((name, norad, sat))

    if not candidates:
        return []

    # Fine pass: sample the exposure only for the handful that came close.
    n = max(2, min(MAX_SAMPLES, int(exposure_s * SAMPLE_HZ) + 1))
    offsets = [exposure_s * i / (n - 1) for i in range(n)]
    times = ts.from_datetimes(
        [when_utc + timedelta(seconds=o) for o in offsets])

    crossings = []
    for name, norad, sat in candidates:
        try:
            ra_deg, dec_deg = _radec_deg(sat, observer, times)
        except Exception:
            continue
        ra_deg, dec_deg = np.asarray(ra_deg), np.asarray(dec_deg)
        near = np.array([
            math.isfinite(r) and math.isfinite(d)
            and _sep_deg(r, d, center[0], center[1]) <= radius + 1.5
            for r, d in zip(ra_deg, dec_deg)])
        if not near.any():
            continue
        idx = np.where(near)[0]
        pixels = wcs.all_world2pix(
            np.column_stack([ra_deg[idx], dec_deg[idx]]), 0)
        in_frame = [(int(i), float(x), float(y))
                    for i, (x, y) in zip(idx, pixels)
                    if math.isfinite(x) and math.isfinite(y)
                    and 0 <= x < width and 0 <= y < height]
        if not in_frame:
            continue
        crossings.append({
            "name": name,
            "norad_id": norad,
            "points": [[round(x, 1), round(y, 1)] for _, x, y in in_frame],
            "t_enter_s": round(offsets[in_frame[0][0]], 1),
            "t_exit_s": round(offsets[in_frame[-1][0]], 1),
        })

    # Longest track first: the most likely to have left a visible streak.
    crossings.sort(key=lambda c: len(c["points"]), reverse=True)
    return crossings[:MAX_CROSSINGS]


def annotate(wcs_path, width, height, exif_info, fetch=_fetch):
    """Satellite layer for a solved image: {"crossings": [...], ...} or
    {"skipped": reason}. May raise (network, bad TLE data) — the worker
    treats the whole layer as best-effort."""
    when_utc, _ = ephemeris.resolve_utc(exif_info)
    if when_utc is None:
        return {"skipped": "no_timestamp"}
    lat, lon = exif_info.get("lat"), exif_info.get("lon")
    if lat is None or lon is None:
        # Without an observer, low-orbit positions would be fiction.
        return {"skipped": "no_gps"}

    records, cache_path, source = _tle_records(when_utc, fetch=fetch)
    if records is None:
        return {"skipped": source}

    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)

    exposure = exif_info.get("exposure_seconds") or DEFAULT_EXPOSURE_SECONDS
    crossings = compute_crossings(wcs, width, height, when_utc, exposure,
                                  lat, lon, _satellites(records, cache_path))
    return {"crossings": crossings, "objects_checked": len(records),
            "exposure_seconds": exposure, "source": source}
