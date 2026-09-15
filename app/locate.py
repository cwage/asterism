"""Roughly where on Earth a solve was taken, without GPS (#115).

The solved WCS says which point of the sky sits at every pixel. If the
frame's relation to the horizon were known, the zenith's pixel would be
known, and the zenith's sky coordinates are the observer's latitude and
local sidereal time by definition; with the EXIF clock, sidereal time
minus Greenwich sidereal time is longitude. Two things can supply that
relation:

- The phone's own tilt. iPhones record the gravity vector at capture in
  the MakerNote (exif.apple_gravity). That fixes the zenith's direction
  and distance from the frame centre to the accelerometer's degree or so,
  and the answer comes out to a couple of hundred kilometres. Android
  phones checked (Pixel, Samsung) record nothing of the kind.
- The horizon in the pixels. Measured against GPS truth, a skyline found
  in the frame sat 11 to 31 degrees above the true horizon (a ridge, a
  treeline), and nothing in one frame says which, so latitude bands came
  out 20 degrees wide. Not shipped; the numbers are on the issue.

With GPS the same map just names the place, at the scale the disclosure
already permits.

The copy reads as an estimate, never finer than a country and a compass
half of it (a state or province where the vendored map has them), and
the clock has to agree: the timezone at the estimated point must have
the photo's UTC offset at the photo's instant, or nothing is said.
"""

import json
import math
import os

import numpy as np

from . import ephemeris

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")
COUNTRIES_FILE = "countries.geojson"
ADMIN1_FILE = "admin1.geojson"

TILT_ERROR_DEG = 2.5    # accelerometer plus a handheld phone's wobble
MIN_FORWARD = 0.05      # cos(zenith from camera axis) below this: aimed at the ground
GPS_HALF_DEG = 0.2      # the box named around a GPS fix
TZ_HALF_WIDTH = 7.5

# Natural Earth abbreviates a few names; and some take an article.
COUNTRY_NAMES = {
    "United States of America": "the United States",
    "United Kingdom": "the United Kingdom",
    "Netherlands": "the Netherlands",
    "Philippines": "the Philippines",
    "Bahamas": "the Bahamas",
    "United Arab Emirates": "the United Arab Emirates",
    "Dominican Rep.": "the Dominican Republic",
    "Central African Rep.": "the Central African Republic",
    "Dem. Rep. Congo": "the Democratic Republic of the Congo",
    "Bosnia and Herz.": "Bosnia and Herzegovina",
    "Solomon Is.": "the Solomon Islands",
    "Falkland Is.": "the Falkland Islands",
    "S. Sudan": "South Sudan",
    "W. Sahara": "Western Sahara",
    "Eq. Guinea": "Equatorial Guinea",
    "N. Cyprus": "Northern Cyprus",
    "eSwatini": "Eswatini",
    "Czechia": "Czechia",
}

# The upright image's right and down axes in the phone's frame, by how
# the phone was held. Phones store sensor frames plus an orientation tag,
# but the tag is reset by any tool that rotates the pixels first (an
# export, a messaging app that kept the MakerNote), so the hold is read
# from the tilt itself: which phone axis points skyward, and whether the
# upright frame is portrait or landscape. The pre-bake tag is consulted
# for one thing only: the mirrored values, which the bake flips left to
# right, and which no rear camera writes, so this is belt and braces.
MIRRORED = {2, 4, 5, 7}
_HOLDS = {
    "portrait": ((1, 0, 0), (0, -1, 0)),          # top of the phone up
    "portrait-inverted": ((-1, 0, 0), (0, 1, 0)),
    "landscape-right": ((0, -1, 0), (-1, 0, 0)),  # port to the right: the right side is up
    "landscape-left": ((0, 1, 0), (1, 0, 0)),
}

_maps_cache = {}


# ---- the tilt ----

def hold(up, width, height):
    """How the phone was held, from the up vector in its frame and the
    upright image's shape. A square frame (a crop) can't say, and goes by
    whichever phone axis is nearer the sky; nearly straight up, with no
    in-plane component to speak of, is the same for every hold."""
    ux, uy = float(up[0]), float(up[1])
    portrait = height > width or (height == width and abs(uy) >= abs(ux))
    if portrait:
        return "portrait" if uy >= 0 else "portrait-inverted"
    return "landscape-right" if ux >= 0 else "landscape-left"


def up_in_frame(gravity, width, height, orientation=None):
    """Where the zenith lies relative to the upright image, from the
    phone's gravity vector: ((dx, dy), zeta), a unit direction in image
    coordinates (x right, y down) and the angle in degrees from the frame
    centre. None when the camera was aimed at or below the horizontal
    plane through the axis, which is no sky photo.

    The vector is in the phone's frame (X right, Y up, Z toward the
    user); the rear camera looks along -Z, so the zenith is in front of
    the camera when up has a -Z component. `orientation` is the pre-bake
    tag, used only to undo a mirrored bake's left-right flip."""
    g = np.asarray(gravity, dtype=float)
    norm = float(np.linalg.norm(g))
    if not math.isfinite(norm) or norm == 0.0:
        return None
    up = -g / norm
    forward = -up[2]
    if forward < MIN_FORWARD:
        return None
    right, down = _HOLDS[hold(up, width, height)]
    dx, dy = float(np.dot(up, right)), float(np.dot(up, down))
    if orientation in MIRRORED:
        dx = -dx
    span = math.hypot(dx, dy)
    if span < 1e-9:
        dx, dy = 0.0, -1.0  # straight up: the direction no longer matters
    else:
        dx, dy = dx / span, dy / span
    return (dx, dy), math.degrees(math.acos(min(1.0, forward)))


def _unit(ra, dec):
    r, d = math.radians(ra), math.radians(dec)
    return np.array([math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d)])


def _radec(v):
    v = v / np.linalg.norm(v)
    ra = math.degrees(math.atan2(v[1], v[0])) % 360.0
    dec = math.degrees(math.asin(max(-1.0, min(1.0, float(v[2])))))
    return ra, dec


def zenith(wcs, width, height, gravity, orientation=None):
    """The zenith's (ra, dec): from the frame centre, `zeta` degrees along
    the great circle that leaves the centre in the image direction the
    tilt gives. The WCS supplies both points on that circle, so any
    flip or rotation in the solve is already accounted for."""
    tilt = up_in_frame(gravity, width, height, orientation)
    if tilt is None:
        return None
    (dx, dy), zeta = tilt
    cx, cy = width / 2.0, height / 2.0
    reach = 0.25 * min(width, height)
    (cra, cdec), (pra, pdec) = (
        (float(v[0]), float(v[1])) for v in
        (wcs.all_pix2world(cx, cy, 0),
         wcs.all_pix2world(cx + dx * reach, cy + dy * reach, 0))
    )
    c = _unit(cra, cdec)
    t = _unit(pra, pdec)
    t = t - np.dot(t, c) * c
    if np.linalg.norm(t) < 1e-9:
        return _radec(c)
    t /= np.linalg.norm(t)
    a = math.radians(zeta)
    return _radec(c * math.cos(a) + t * math.sin(a))


def _wrap(lon):
    return ((lon + 180.0) % 360.0) - 180.0


# ---- naming ----

def _load_map(name):
    if name not in _maps_cache:
        with open(os.path.join(CATALOG_DIR, name)) as f:
            data = json.load(f)
        out = []
        for feat in data["features"]:
            geom = feat["geometry"]
            if geom is None:
                continue
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            rings = [np.asarray(poly[0], dtype=float) for poly in polys if poly]
            if not rings:
                continue
            lons = np.concatenate([r[:, 0] for r in rings])
            lats = np.concatenate([r[:, 1] for r in rings])
            props = feat["properties"]
            out.append({"name": props.get("NAME") or props.get("name"),
                        "admin": props.get("admin"), "rings": rings,
                        "bbox": (float(lons.min()), float(lats.min()),
                                 float(lons.max()), float(lats.max()))})
        _maps_cache[name] = out
    return _maps_cache[name]


def _inside(ring, lon, lat):
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    cross = (y > lat) != (y2 > lat)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = x + (lat - y) * (x2 - x) / (y2 - y)
    return bool(np.sum(cross & (lon < xint)) % 2)


def _feature_at(features, lat, lon):
    for f in features:
        x0, y0, x1, y1 = f["bbox"]
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        if any(_inside(r, lon, lat) for r in f["rings"]):
            return f
    return None


def region_at(lat, lon):
    """(country feature, state/province feature or None) for a point;
    (None, None) at sea. The province layer covers the big federations
    only, which is where a state name is what people say."""
    lon = _wrap(lon)
    country = _feature_at(_load_map(COUNTRIES_FILE), lat, lon)
    if country is None:
        return None, None
    return country, _feature_at(_load_map(ADMIN1_FILE), lat, lon)


def _part(feature, lat, lon):
    """'northern', 'southern', 'eastern', 'western' or '' for a point in a
    feature, by where it sits in the feature's extent."""
    x0, y0, x1, y1 = feature["bbox"]
    fy = (lat - y0) / max(1e-6, y1 - y0)
    fx = (lon - x0) / max(1e-6, x1 - x0)
    if y1 - y0 >= 4.0 and (fy > 0.67 or fy < 0.33):
        return "northern" if fy > 0.67 else "southern"
    if x1 - x0 >= 4.0 and (fx > 0.67 or fx < 0.33):
        return "eastern" if fx > 0.67 else "western"
    return ""


def name_box(lat, lon, half_lat, half_lon):
    """Words for a box of the map: 'northern Pakistan or Tajikistan',
    'Tennessee or Kentucky, in the United States'. Returns (phrase or
    None, fraction of the box on open water)."""
    # Samples nearer the estimate count for more: the box is an error
    # bar, and its centre is the answer, so the state under the centre
    # leads even when a neighbour covers more of the box's edges.
    n = 5
    counts = {}
    total = 0.0
    sea = 0.0
    for la in np.linspace(lat - half_lat, lat + half_lat, n):
        for lo in np.linspace(lon - half_lon, lon + half_lon, n):
            weight = math.exp(-2.0 * (((la - lat) / half_lat) ** 2 + ((lo - lon) / half_lon) ** 2))
            total += weight
            country, admin = region_at(float(la), float(lo))
            if country is None:
                sea += weight
                continue
            key = (country["name"], admin["name"] if admin else None)
            counts[key] = counts.get(key, 0.0) + weight
    land = sum(counts.values())
    if not land:
        return None, 1.0
    by_country = {}
    for (cname, aname), k in counts.items():
        entry = by_country.setdefault(cname, {"n": 0, "admin": {}})
        entry["n"] += k
        if aname:
            entry["admin"][aname] = entry["admin"].get(aname, 0) + k
    ranked = sorted(by_country.items(), key=lambda kv: -kv[1]["n"])
    kept = [kv for kv in ranked if kv[1]["n"] / land >= 0.15][:3] or ranked[:1]
    parts = []
    for cname, entry in kept:
        shown = COUNTRY_NAMES.get(cname, cname)
        admins = sorted(entry["admin"].items(), key=lambda kv: -kv[1])
        admins = [a for a, k in admins if k / entry["n"] >= 0.2][:2]
        if admins:
            parts.append(" or ".join(admins) + ", in " + shown)
        else:
            country = next(f for f in _load_map(COUNTRIES_FILE) if f["name"] == cname)
            part = _part(country, lat, _wrap(lon))
            parts.append((part + " " if part else "") + shown)
    return " or ".join(parts), round(sea / total, 2)


# ---- the estimate ----

def _clock_agrees(lat, lon, half_lat, half_lon, when_utc, delta):
    """Whether the timezone at the estimate (or a corner of its box) has
    the photo's UTC offset at the photo's instant. Falls back to the
    offset's longitude band when the zone lookup can't answer."""
    from zoneinfo import ZoneInfo

    for la, lo in ((lat, lon), (lat - half_lat, lon - half_lon), (lat - half_lat, lon + half_lon),
                   (lat + half_lat, lon - half_lon), (lat + half_lat, lon + half_lon)):
        try:
            zone = ephemeris._zone_from_gps(la, _wrap(lo))
            if not zone:
                continue
            offset = when_utc.astimezone(ZoneInfo(zone)).utcoffset()
            if abs((offset - delta).total_seconds()) < 60:
                return True, zone
        except Exception:
            continue
    band = ephemeris.guess_longitudes(delta)
    lo, hi = min(band) - TZ_HALF_WIDTH - half_lon, max(band) + TZ_HALF_WIDTH + half_lon
    return lo <= _wrap(lon) <= hi, None


def _lat_str(lat):
    return f"{abs(round(lat))}°{'N' if lat >= 0 else 'S'}"


def _lon_str(lon):
    lon = _wrap(lon)
    return f"{abs(round(lon))}°{'E' if lon >= 0 else 'W'}"


def describe(place):
    if not place or place.get("reason"):
        return None
    where = place.get("regions") or "open water on this map"
    if place["source"] == "gps":
        return f"Photographed from {where}." if place.get("regions") else None
    return (f"The phone recorded its tilt, so sky geometry puts this near "
            f"{_lat_str(place['lat'])}, {_lon_str(place['lon'])}: {where}.")


def estimate(wcs_path, width, height, exif_info):
    """The place dict, or None when there is nothing to go on."""
    lat, lon = exif_info.get("lat"), exif_info.get("lon")
    if lat is not None and lon is not None:
        regions, sea = name_box(lat, lon, GPS_HALF_DEG, GPS_HALF_DEG)
        return {"source": "gps", "lat": round(lat, 1), "lon": round(lon, 1),
                "error_deg": GPS_HALF_DEG, "regions": regions,
                "sea": sea, "reason": None}

    gravity = exif_info.get("gravity")
    if not gravity:
        return None
    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)
    z = zenith(wcs, width, height, gravity, exif_info.get("orientation"))
    if z is None:
        return {"source": "tilt", "reason": "camera aimed below the horizontal"}
    ra, dec = z
    # The zone the clock check resolved stays out of the dict: the job
    # record is public, and the estimate is the only location it carries.
    out = {"source": "tilt", "lat": round(dec, 1), "lon": None,
           "error_deg": TILT_ERROR_DEG, "regions": None,
           "sea": None, "reason": None}
    when_utc, source = ephemeris.resolve_utc(exif_info)
    delta = ephemeris._parse_offset(exif_info.get("offset_time_original"))
    if when_utc is None or delta is None or source != "exif_offset":
        out["reason"] = "no clock offset"
        return out
    gmst = float(ephemeris._timescale().from_datetime(when_utc).gmst) * 15.0
    lon = _wrap(ra - gmst)
    out["lon"] = round(lon, 1)
    half_lon = TILT_ERROR_DEG / max(0.2, math.cos(math.radians(dec)))
    agrees, _zone = _clock_agrees(dec, lon, TILT_ERROR_DEG, half_lon, when_utc, delta)
    if not agrees:
        out["reason"] = "clock disagrees with the tilt"
        return out
    out["regions"], out["sea"] = name_box(dec, lon, TILT_ERROR_DEG, half_lon)
    return out


def annotate(wcs_path, width, height, exif_info):
    """The place dict with its sentence, or None when nothing can be said."""
    place = estimate(wcs_path, width, height, exif_info)
    if place is None:
        return None
    place["line"] = describe(place)
    return place if place["line"] else None
