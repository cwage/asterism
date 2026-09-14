"""Bright objects just outside the frame (#118).

Every layer projects its catalog through the solved WCS and drops whatever
lands off-frame, so the answer to the most common follow-up question —
"what was that bright thing I didn't get in the shot?" — is computed and
thrown away on every solve. This keeps the bright end of it: stars to
magnitude 2, the Moon and planets always, and the showpiece deep-sky
objects the DSO layer already names, whenever one sits within MAX_EDGE_DEG
of the nearest edge.

Projection uses the plain gnomonic core of the WCS (wcs_world2pix, not
all_world2pix): the SIP distortion polynomial is fitted inside the frame
and diverges outside it. The pointer sits where the line from frame centre
to the object crosses the edge, and the distance quoted is the true sky
separation between that crossing point and the object, so it does not
inherit the projection's stretch. Anything further than MAX_TANGENT_DEG
from the tangent point is skipped outright: past that the projection
stretches without limit and a pixel position means nothing.

Directions are in the photo's own terms — left, right, above, below as
displayed (the orientation is baked in before the solve) — never the
sky's."""

import math

from . import dso, ephemeris, solver

STAR_MAG_LIMIT = 2.0
MAX_EDGE_DEG = 15.0
MAX_TANGENT_DEG = 60.0
MAX_POINTERS = 6
MAX_PER_SIDE = 3


def _separation_deg(ra1, dec1, ra2, dec2):
    """Great-circle separation in degrees, Vincenty form: exact at every
    angle, where the haversine loses precision near 0 and 180."""
    a1, d1, a2, d2 = (math.radians(v) for v in (ra1, dec1, ra2, dec2))
    da = a2 - a1
    sd1, cd1, sd2, cd2 = math.sin(d1), math.cos(d1), math.sin(d2), math.cos(d2)
    num = math.hypot(cd2 * math.sin(da), cd1 * sd2 - sd1 * cd2 * math.cos(da))
    den = sd1 * sd2 + cd1 * cd2 * math.cos(da)
    return math.degrees(math.atan2(num, den))


def _edge_crossing(cx, cy, x, y, width, height):
    """Where the ray from frame centre toward an off-frame (x, y) leaves
    the frame: (edge_x, edge_y, side). The smallest positive step to any
    edge is the one the ray reaches first."""
    dx, dy = x - cx, y - cy
    best, side = math.inf, None
    if dx > 0 and (width - cx) / dx < best:
        best, side = (width - cx) / dx, "right"
    elif dx < 0 and -cx / dx < best:
        best, side = -cx / dx, "left"
    if dy > 0 and (height - cy) / dy < best:
        best, side = (height - cy) / dy, "below"
    elif dy < 0 and -cy / dy < best:
        best, side = -cy / dy, "above"
    return cx + dx * best, cy + dy * best, side


def _bodies(exif_info):
    """The Moon and planets for the photo's instant, under the same clock
    and observer rules as the in-frame body layer; nothing when the photo
    has no usable timestamp."""
    when_utc, _ = ephemeris.resolve_utc(exif_info)
    if when_utc is None:
        return []
    try:
        lat, lon = ephemeris.observer_latlon(exif_info)
        return ephemeris.compute_bodies(when_utc, lat, lon)
    except FileNotFoundError:  # no ephemeris file: the body layer said so
        return []


def candidates(exif_info):
    """The bright end of every catalog, as {name, kind, mag, ra, dec}."""
    out = []
    for star in solver.load_catalog():  # brightest first
        if star["mag"] > STAR_MAG_LIMIT:
            break
        out.append({"name": star["name"], "kind": "star", "mag": star["mag"],
                    "ra": star["ra"], "dec": star["dec"]})
    for body in _bodies(exif_info):
        out.append({"name": body["name"], "kind": body["kind"],
                    "mag": body.get("mag"), "ra": body["ra"], "dec": body["dec"]})
    showpieces = set(dso.DISPLAY_NAMES.values())
    for obj in dso.load_catalog():
        if obj["name"] in showpieces:
            out.append({"name": obj["name"], "kind": "dso", "mag": obj["mag"],
                        "ra": obj["ra"], "dec": obj["dec"]})
    return out


def format_deg(deg):
    """'8°', '0.4°': whole degrees, except under one degree where a tenth
    is the difference between "on the edge" and "just past it". Ties round
    half-up, so the card and the narration agree with the page, whose
    Math.round does the same (Python's round is ties-to-even)."""
    if deg < 1:
        return f"{deg:.1f}°"
    return f"{math.floor(deg + 0.5)}°"


def _priority(pointer):
    # The Moon, planets and DSOs are few and notable; stars brightest-first.
    tier = {"moon": -3, "planet": -2, "dso": -1}.get(pointer["kind"], 0)
    return tier * 100 + (pointer["mag"] if pointer["mag"] is not None else 0)


def annotate(wcs_path, width, height, exif_info):
    """Pointers for bright objects just outside the frame, most notable
    first, at most MAX_POINTERS and MAX_PER_SIDE on any one edge. Each is
    {name, kind, mag, edge_x, edge_y, ux, uy, deg, side}: the edge
    crossing in pixels, the unit vector from it toward the object, the sky
    separation from the crossing to the object, and which edge."""
    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)
    ra0, dec0 = (float(v) for v in wcs.wcs.crval)
    cx, cy = width / 2.0, height / 2.0

    pointers = []
    for obj in candidates(exif_info):
        if _separation_deg(ra0, dec0, obj["ra"], obj["dec"]) > MAX_TANGENT_DEG:
            continue
        try:
            x, y = wcs.wcs_world2pix(obj["ra"], obj["dec"], 0)
            x, y = float(x), float(y)
        except Exception:
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        if 0 <= x < width and 0 <= y < height:
            continue  # in the frame: the layers above label it
        ex, ey, side = _edge_crossing(cx, cy, x, y, width, height)
        # The crossing point lies inside the fitted region, so the
        # distortion-aware transform is the accurate one for it.
        era, edec = wcs.all_pix2world(ex, ey, 0)
        deg = _separation_deg(float(era), float(edec), obj["ra"], obj["dec"])
        if deg > MAX_EDGE_DEG:
            continue
        run = math.hypot(x - ex, y - ey) or 1.0
        pointers.append({
            "name": obj["name"], "kind": obj["kind"], "mag": obj["mag"],
            "edge_x": round(ex, 1), "edge_y": round(ey, 1),
            "ux": round((x - ex) / run, 3), "uy": round((y - ey) / run, 3),
            "deg": round(deg, 1), "side": side,
        })

    pointers.sort(key=_priority)
    kept, per_side = [], {}
    for p in pointers:
        if per_side.get(p["side"], 0) >= MAX_PER_SIDE:
            continue
        per_side[p["side"]] = per_side.get(p["side"], 0) + 1
        kept.append(p)
        if len(kept) >= MAX_POINTERS:
            break
    return kept
