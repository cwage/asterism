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
_tzf_cache = None


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


def _parse_offset(offset):
    """EXIF OffsetTime* string ('+02:00', '-0500', 'Z') -> timedelta or None."""
    if not offset:
        return None
    offset = offset.strip()
    if offset == "Z":
        return timedelta(0)
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", offset)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


def _zone_from_gps(lat, lon):
    """IANA zone name for the coordinates, from timezonefinder's offline
    index (covers oceans as Etc/GMT+N zones). None when lookup fails."""
    global _tzf_cache
    from timezonefinder import TimezoneFinder

    if _tzf_cache is None:
        _tzf_cache = TimezoneFinder()
    return _tzf_cache.timezone_at(lat=lat, lng=lon)


def resolve_utc(exif_info):
    """Best-effort UTC instant for the exposure. Returns (datetime, source).

    EXIF DateTimeOriginal is local time with no zone. Preference order (#6):
    OffsetTimeOriginal when present, else the IANA zone at the GPS fix
    (DST-correct for the photo's date), else a crude zone guess from GPS
    longitude (right to within an hour or two — the Moon moves 0.55 deg/hr,
    planets far less), else assume UTC. The source string lets the UI
    surface whatever assumption was made.
    """
    dto = exif_info.get("datetime_original")
    if not dto:
        return None, None
    try:
        naive = datetime.strptime(dto.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, None

    delta = _parse_offset(exif_info.get("offset_time_original"))
    if delta is not None:
        local = naive.replace(tzinfo=timezone(delta))
        return local.astimezone(timezone.utc), "exif_offset"

    lat, lon = exif_info.get("lat"), exif_info.get("lon")
    if lat is not None and lon is not None:
        try:
            from zoneinfo import ZoneInfo

            zone = _zone_from_gps(lat, lon)
            if zone:
                local = naive.replace(tzinfo=ZoneInfo(zone))
                return local.astimezone(timezone.utc), "gps_timezone"
        except Exception:
            pass  # unknown zone or lookup failure: fall through to cruder guesses

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


# ---- no-solve fallback (#7) ----

# Horizon cut for the guess (#80). This used to sit at 3 degrees on the theory
# that anything lower is behind trees or haze. It isn't: a 7%-lit crescent Moon
# photographed over a rooftop on 2026-08-14 computed to +2.7 degrees at the
# capture location and was dropped, from a frame it is plainly visible in.
# Refraction is already in the apparent() positions, so 0 means what it says.
FALLBACK_MIN_ALT_DEG = 0.0
# No GPS (phones drop it while still recording heading): a mid-northern
# latitude gets above/below-horizon roughly right. Flagged so the UI can hedge.
FALLBACK_GUESS_LAT = 38.0


def _wrap_lon(lon):
    return ((lon + 180.0) % 360.0) - 180.0


def guess_longitudes(delta):
    """Longitudes consistent with a UTC offset, westmost first (#79).

    `offset * 15` is the meridian only under *standard* time. Under daylight
    saving the clock runs an hour ahead of the sun, so the same offset belongs
    to a zone 15 degrees further west — and EXIF never says which applies.
    Both hypotheses are returned, and a body counts as visible if it clears
    the horizon under either.

    Getting this wrong is not subtle: a photo taken at 20:28 CDT put the Moon
    at -6.6 degrees under the standard-time reading and +5.2 under the daylight
    one, which is the difference between "below the horizon" and the crescent
    the photographer was looking at."""
    hours = delta.total_seconds() / 3600.0
    return [_wrap_lon((hours - 1.0) * 15.0), _wrap_lon(hours * 15.0)]


def _band_center(longitudes):
    """Point estimate for a set of candidate longitudes: their midpoint, which
    is also the middle of the timezone band they span."""
    if len(longitudes) == 1:
        return longitudes[0]
    return _wrap_lon(longitudes[0] + _wrap_lon(longitudes[-1] - longitudes[0]) / 2.0)

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(az):
    return _COMPASS[round(az / 22.5) % 16]


def _declination(lat, lon, when_utc):
    """Magnetic declination in degrees (+E) from the World Magnetic Model.
    EXIF headings are almost always magnetic; leaving this uncorrected is a
    ~5 deg error in the eastern US and >15 deg in places."""
    from pygeomag import GeoMag

    year = when_utc.year + (when_utc.timetuple().tm_yday - 1) / 365.25
    return float(GeoMag().calculate(glat=lat, glon=lon, alt=0, time=year).d)


def fallback_guess(exif_info):
    """When the solve fails, answer the question anyway: what was above the
    horizon at the EXIF instant, and where relative to the compass heading
    the phone recorded (#7). Returns None without a usable timestamp or any
    location hint; may raise (missing ephemeris file) — the worker treats
    the whole thing as best-effort."""
    when_utc, time_source = resolve_utc(exif_info)
    if when_utc is None:
        return None

    lat, lon = exif_info.get("lat"), exif_info.get("lon")
    location_source = "gps"
    longitudes = [lon]
    if lat is None or lon is None:
        delta = _parse_offset(exif_info.get("offset_time_original"))
        if delta is None:
            return None  # no location and no zone: alt/az would be fiction
        lat = FALLBACK_GUESS_LAT
        longitudes = guess_longitudes(delta)
        lon = _band_center(longitudes)
        location_source = "timezone_guess"

    guess = {"time_utc": when_utc.isoformat(), "time_source": time_source,
             "location_source": location_source}

    heading_true = None
    heading = exif_info.get("heading")
    if heading is not None:
        ref = (exif_info.get("heading_ref") or "M").upper()
        if ref.startswith("T"):
            heading_true = heading % 360.0
        else:
            try:
                decl = _declination(lat, lon, when_utc)
            except Exception:
                decl = None  # uncorrected magnetic beats no heading at all
            heading_true = (heading + (decl or 0.0)) % 360.0
            if decl is not None:
                guess["declination_deg"] = round(decl, 1)
        guess["heading_true"] = round(heading_true, 1)

    from skyfield import almanac
    from skyfield.api import wgs84
    from skyfield.magnitudelib import planetary_magnitude

    eph = load_ephemeris()
    t = _timescale().from_datetime(when_utc)
    # The band centre carries the headline numbers; the hypotheses either side
    # of it decide what counts as visible at all (#79).
    at = (eph["earth"] + wgs84.latlon(lat, lon)).at(t)
    views = [(eph["earth"] + wgs84.latlon(lat, l)).at(t) for l in longitudes]

    # Sun altitude gives the failure its context: daylight shot, twilight
    # washing out the stars, or genuinely dark sky.
    sun_alt, _, _ = at.observe(eph["sun"]).apparent().altaz()
    guess["sun_alt_deg"] = round(float(sun_alt.degrees))

    candidates = []
    for name, key in BODIES:
        pos = at.observe(eph[key]).apparent()
        alt, az, _ = pos.altaz()
        alt, az = float(alt.degrees), float(az.degrees)
        # A timezone guess spans a whole zone, and near the horizon that is
        # worth several degrees of altitude either way. Judge visibility on
        # the best case rather than dropping anything the midpoint happens to
        # put below the horizon.
        alts = [float(v.observe(eph[key]).apparent().altaz()[0].degrees)
                for v in views]
        if max(alts) < FALLBACK_MIN_ALT_DEG:
            continue
        cand = {"name": name, "alt_deg": round(alt), "az_deg": round(az),
                "direction": _compass(az),
                "kind": "moon" if key == "moon" else "planet"}
        if len(alts) > 1:
            # How much of the spread is the location guess rather than the sky.
            cand["alt_range_deg"] = [round(min(alts)), round(max(alts))]
        try:
            cand["mag"] = round(float(planetary_magnitude(pos)), 1)
        except Exception:
            cand["mag"] = None  # the Moon; phase carries the brightness story
        if key == "moon":
            cand["phase"] = round(
                float(almanac.fraction_illuminated(eph, "moon", t)), 3)
        if heading_true is not None:
            cand["offset_deg"] = round(((az - heading_true + 180.0) % 360.0)
                                       - 180.0)
        candidates.append(cand)

    # Brightest first; the Moon (no magnitude) outshines everything.
    candidates.sort(key=lambda c: c["mag"] if c["mag"] is not None else -99.0)
    guess["candidates"] = candidates
    return guess
