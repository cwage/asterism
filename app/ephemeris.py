"""Ephemeris layer: label the things astrometry.net structurally cannot.

Computes Moon/planet ICRS positions for the photo's EXIF instant (topocentric
when GPS is present) and projects them through the solved WCS, same as the
star catalog. Positions come from the JPL DE421 ephemeris via skyfield.
"""

import os
import re
from datetime import datetime, timedelta, timezone

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")
EPHEMERIS_FILE = "de421.bsp"

# DE421 only carries barycenters for the outer planets; the offset from the
# planet center is far below labeling accuracy. Naked-eye set only.
BODIES = [
    ("Moon", "moon"),
    ("Mercury", "mercury"),
    ("Venus", "venus"),
    ("Mars", "mars barycenter"),
    ("Jupiter", "jupiter barycenter"),
    ("Saturn", "saturn barycenter"),
]

_eph_cache = None
_ts_cache = None


def load_ephemeris():
    global _eph_cache
    if _eph_cache is None:
        from skyfield.api import load_file

        _eph_cache = load_file(os.path.join(CATALOG_DIR, EPHEMERIS_FILE))
    return _eph_cache


def _timescale():
    global _ts_cache
    if _ts_cache is None:
        from skyfield.api import load

        _ts_cache = load.timescale()
    return _ts_cache


def resolve_utc(exif_info):
    """Best-effort UTC instant for the exposure. Returns (datetime, source).

    EXIF DateTimeOriginal is local time with no zone. Preference order:
    OffsetTimeOriginal when present, else a crude zone guess from GPS
    longitude (right to within an hour or two — the Moon moves 0.55 deg/hr,
    planets far less), else assume UTC. Proper zone lookup is issue #6.
    """
    dto = exif_info.get("datetime_original")
    if not dto:
        return None, None
    try:
        naive = datetime.strptime(dto.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, None

    offset = exif_info.get("offset_time_original")
    if offset:
        m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", offset.strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            local = naive.replace(tzinfo=timezone(sign * delta))
            return local.astimezone(timezone.utc), "exif_offset"
        if offset.strip() in ("Z", "+00:00", "-00:00"):
            return naive.replace(tzinfo=timezone.utc), "exif_offset"

    lon = exif_info.get("lon")
    if lon is not None:
        delta = timedelta(hours=round(lon / 15.0))
        local = naive.replace(tzinfo=timezone(delta))
        return local.astimezone(timezone.utc), "gps_longitude"

    return naive.replace(tzinfo=timezone.utc), "assumed_utc"


def compute_bodies(when_utc, lat=None, lon=None):
    """ICRS positions (and Moon illumination) for the naked-eye bodies.

    Topocentric when lat/lon are given — parallax shifts the Moon by up to
    a degree; for everything else the observer position barely matters.
    """
    from skyfield import almanac
    from skyfield.api import wgs84
    from skyfield.magnitudelib import planetary_magnitude

    eph = load_ephemeris()
    t = _timescale().from_datetime(when_utc)
    observer = eph["earth"]
    if lat is not None and lon is not None:
        observer = observer + wgs84.latlon(lat, lon)

    bodies = []
    for name, key in BODIES:
        pos = observer.at(t).observe(eph[key]).apparent()
        ra, dec, _ = pos.radec()  # ICRS, matching the solved WCS
        body = {
            "name": name,
            "ra": float(ra.hours) * 15.0,
            "dec": float(dec.degrees),
            "kind": "moon" if key == "moon" else "planet",
        }
        try:
            body["mag"] = round(float(planetary_magnitude(pos)), 2)
        except Exception:
            body["mag"] = None
        if key == "moon":
            body["phase"] = round(float(almanac.fraction_illuminated(eph, "moon", t)), 3)
        bodies.append(body)
    return bodies


def annotate_bodies(wcs_path, width, height, exif_info):
    """Project the bodies through the solved WCS. Returns (labels, meta).

    Never raises on missing time/ephemeris data — labels are best-effort
    extras on top of the star solve.
    """
    when_utc, source = resolve_utc(exif_info)
    meta = {"time_utc": when_utc.isoformat() if when_utc else None,
            "time_source": source}
    if when_utc is None:
        return [], meta

    try:
        bodies = compute_bodies(when_utc, exif_info.get("lat"), exif_info.get("lon"))
    except FileNotFoundError:
        meta["error"] = "ephemeris file missing"
        return [], meta

    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)

    labels = []
    for body in bodies:
        try:
            x, y = wcs.all_world2pix(body["ra"], body["dec"], 0)
            x, y = float(x), float(y)
        except Exception:
            continue
        if not (0 <= x < width and 0 <= y < height):
            continue
        label = {"name": body["name"], "x": round(x, 1), "y": round(y, 1),
                 "mag": body["mag"], "kind": body["kind"]}
        if "phase" in body:
            label["phase"] = body["phase"]
            meta["moon_phase"] = body["phase"]
        labels.append(label)
    return labels, meta
