"""What the night was like (#121), and how deep the photo reached (#122).

A solved photo with a timestamp carries the explanation people most often
want from a labelled image and rarely get: not what the dots are, but why
the photo looks the way it does. The Moon's illumination is already
computed for the label layer; this adds the Sun's altitude (twilight or
full dark), whether the Moon was up, and, from the verification pass, the
faintest magnitude the phone actually recorded. Each becomes one plain
sentence, rendered here so the page, the copied description and the
narration all quote the same words.

Location honesty. With GPS the Sun and Moon altitudes are exact. With only
a clock offset the observer could be anywhere in a timezone band, so both
are evaluated across a grid of plausible latitudes under each of the
offset's two longitude hypotheses (#79). Two facts prune that grid: the
solved field was above the horizon, and stars were photographed, so the
Sun was below it. A statement is made only when every surviving position
agrees; otherwise the sentence is left unsaid rather than guessed. Near
the equinoxes the Sun's altitude at a given clock time barely depends on
latitude, so twilight usually resolves without GPS.
"""

import math
from datetime import datetime, timedelta

import numpy as np

from . import ephemeris

DARK_ALT_DEG = -18.0            # astronomical twilight ends: full dark
LAT_SAMPLES = range(-55, 56, 5)  # where a phone photographer plausibly stands
WINDOW_MINUTES = 12 * 60        # how far to look for the full-dark crossing
STEP_MINUTES = 4                # the answer is rounded to 5; measured 0.6s
                                # for the 46-position grid at this step
MINUTES_SPREAD_MAX = 20         # quote a number only when the samples agree
MINUTES_ROUND = 5

# Illuminated fraction where a phase name starts. "new" and "full" cover
# a couple of days each side of the instant, which is how people use them.
MOON_NEW = 0.04
MOON_QUARTER = 0.40
MOON_GIBBOUS = 0.60
MOON_FULL = 0.96
MOON_BRIGHT = 0.5               # from here up the Moon washes out faint stars

# What a limiting magnitude means to the eye. Rough, deliberately: the
# naked-eye limit runs from about 3 in a city centre to 6.5 under a truly
# dark sky, and a phone's night mode sits somewhere in the same range.
DEPTH_BANDS = [
    (3.5, "the bright ones that show even from a city"),
    (5.0, "about what the eye picks out from the suburbs"),
    (6.5, "right around the naked-eye limit, which takes a dark sky"),
    (math.inf, "fainter than the unaided eye can see"),
]


def _altitude_deg(ra, dec, lat, lon, gmst_hours):
    """Altitude of a sky point from an observer, plain spherical trig —
    no refraction, which is nothing next to a whole-degree sample grid."""
    ha = math.radians(gmst_hours * 15.0 + lon - ra)
    lat_r, dec_r = math.radians(lat), math.radians(dec)
    s = (math.sin(lat_r) * math.sin(dec_r)
         + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha))
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def observers(exif_info, centre, when_utc):
    """Where the photographer could have been: [(lat, lon)] and a source.

    GPS settles it. Otherwise every sampled latitude under both longitude
    hypotheses of the clock offset, minus the positions from which the
    solved field (its centre, `centre` = (ra, dec)) was below the horizon.
    No offset either: nothing, and the caller says nothing that depends on
    where the phone was."""
    lat, lon = exif_info.get("lat"), exif_info.get("lon")
    if lat is not None and lon is not None:
        return [(lat, lon)], "gps"
    delta = ephemeris._parse_offset(exif_info.get("offset_time_original"))
    if delta is None:
        return [], None
    gmst = float(ephemeris._timescale().from_datetime(when_utc).gmst)
    ra, dec = centre
    positions = [
        (float(la), lo)
        for la in LAT_SAMPLES for lo in ephemeris.guess_longitudes(delta)
        if _altitude_deg(ra, dec, la, lo, gmst) > 0.0
    ]
    return positions, "timezone_guess"


def _sun_track(eph, ts, when_utc, lat, lon):
    """Sun altitude in degrees at STEP_MINUTES intervals across
    ±WINDOW_MINUTES, and the index of the exposure instant."""
    from skyfield.api import wgs84

    minutes = np.arange(-WINDOW_MINUTES, WINDOW_MINUTES + 1, STEP_MINUTES)
    t = ts.utc(when_utc.year, when_utc.month, when_utc.day, when_utc.hour,
               when_utc.minute + minutes, when_utc.second)
    place = eph["earth"] + wgs84.latlon(lat, lon)
    alt, _, _ = place.at(t).observe(eph["sun"]).apparent().altaz()
    return np.asarray(alt.degrees, dtype=float), len(minutes) // 2


def _dark_crossing(track, mid):
    """(dusk, minutes) — whether the Sun is setting at the instant, and
    how many minutes to (dusk) or since (dawn) the full-dark crossing.
    minutes is None when the Sun never reaches full dark in the window."""
    dusk = bool(track[mid + 1] < track[mid])
    if dusk:
        later = np.where(track[mid:] <= DARK_ALT_DEG)[0]
        minutes = int(later[0]) * STEP_MINUTES if len(later) else None
    else:
        earlier = np.where(track[:mid + 1] <= DARK_ALT_DEG)[0]
        minutes = (mid - int(earlier[-1])) * STEP_MINUTES if len(earlier) else None
    return dusk, minutes


def _round_minutes(lo, hi):
    if hi - lo > MINUTES_SPREAD_MAX:
        return None
    mid = (lo + hi) / 2.0
    return int(max(MINUTES_ROUND, round(mid / MINUTES_ROUND) * MINUTES_ROUND))


def _sun_tracks(eph, ts, when_utc, positions):
    """[(position, track, mid)] for the positions where the Sun was
    down. The photo has stars in it, so wherever it was taken, it was not
    taken in daylight: a candidate position that says otherwise is wrong,
    and the Moon must not be judged from it either."""
    kept = []
    for lat, lon in positions:
        track, mid = _sun_track(eph, ts, when_utc, lat, lon)
        if track[mid] < 0.0:
            kept.append(((lat, lon), track, mid))
    return kept


def _sun(tracks, fixed):
    """The Sun's state across the surviving observers, or None when they
    can't agree. `fixed` says the position is known (GPS), which is the
    only case that can honestly claim the sky never got dark."""
    if not tracks:
        return None
    alts = [t[m] for _, t, m in tracks]
    lo, hi = min(alts), max(alts)
    out = {"alt_range_deg": [round(lo), round(hi)], "darkness": None,
           "dusk": None, "minutes_to_dark": None, "minutes_since_dark": None,
           "never_dark": False}
    if hi <= DARK_ALT_DEG:
        out["darkness"] = "night"
        return out
    if lo <= DARK_ALT_DEG:
        return out  # some positions dark, some not: unsure
    out["darkness"] = "twilight"
    crossings = [_dark_crossing(t, m) for _, t, m in tracks]
    dusks = {d for d, _ in crossings}
    if len(dusks) != 1:
        return out
    out["dusk"] = dusks.pop()
    minutes = [m for _, m in crossings]
    if any(m is None for m in minutes):
        # Only a fixed position can honestly say the sky never got dark;
        # a grid mixing "never" with numbers just can't quote one.
        out["never_dark"] = fixed
        return out
    n = _round_minutes(min(minutes), max(minutes))
    out["minutes_to_dark" if out["dusk"] else "minutes_since_dark"] = n
    return out


def _moon_name(illuminated, waxing):
    if illuminated < MOON_NEW:
        return "new"
    if illuminated >= MOON_FULL:
        return "full"
    if illuminated < MOON_QUARTER:
        return "waxing crescent" if waxing else "waning crescent"
    if illuminated < MOON_GIBBOUS:
        return "first quarter" if waxing else "last quarter"
    return "waxing gibbous" if waxing else "waning gibbous"


def _moon(eph, ts, when_utc, positions, labels, pointers):
    from skyfield import almanac
    from skyfield.api import wgs84

    t = ts.from_datetime(when_utc)
    illuminated = float(almanac.fraction_illuminated(eph, "moon", t))
    waxing = float(almanac.moon_phase(eph, t).degrees) < 180.0
    up = None
    if positions:
        alts = []
        for lat, lon in positions:
            place = eph["earth"] + wgs84.latlon(lat, lon)
            alt, _, _ = place.at(t).observe(eph["moon"]).apparent().altaz()
            alts.append(float(alt.degrees))
        if min(alts) > 0.0:
            up = True
        elif max(alts) < 0.0:
            up = False
    return {
        "illuminated": round(illuminated, 3),
        "waxing": waxing,
        "name": _moon_name(illuminated, waxing),
        "up": up,
        "in_frame": any(l.get("kind") == "moon" and l.get("status") != "hidden"
                        for l in labels or []),
        "near_frame": any(p.get("kind") == "moon" for p in pointers or []),
    }


def _local_month(exif_info):
    try:
        return datetime.strptime(exif_info["datetime_original"].strip(),
                                 "%Y:%m:%d %H:%M:%S").strftime("%B")
    except (KeyError, ValueError, AttributeError):
        return None


def _exposure_phrase(seconds):
    """'10-second', '2.5-second', '1/4-second'; None without a value."""
    if not seconds or seconds <= 0:
        return None
    if seconds >= 1:
        n = round(seconds, 1)
        return f"{int(n) if n == int(n) else n}-second"
    return f"1/{max(1, round(1.0 / seconds))}-second"


def _moon_phrase(moon):
    name = moon["name"]
    if name in ("new", "full"):
        return name
    pct = round(moon["illuminated"] * 100)
    article = "at" if "quarter" in name else "a"
    return f"{article} {name}, {pct}% lit"


def _sun_line(sun, month):
    if not sun or sun["darkness"] != "twilight":
        return None
    if sun["dusk"] is None:
        return "Taken in twilight rather than full dark."
    if sun["dusk"]:
        if sun["minutes_to_dark"]:
            return (f"Taken in twilight, about {sun['minutes_to_dark']} "
                    "minutes before the sky was fully dark.")
        if sun["never_dark"] and month:
            return (f"Taken in twilight; at this latitude in {month} the "
                    "sky never gets fully dark.")
        return "Taken in twilight, before the sky was fully dark."
    if sun["minutes_since_dark"]:
        return (f"Taken in morning twilight, about {sun['minutes_since_dark']} "
                "minutes after full dark ended.")
    if sun["never_dark"] and month:
        return (f"Taken in morning twilight; at this latitude in {month} "
                "the sky never gets fully dark.")
    return "Taken in morning twilight, after full dark had ended."


def _moon_line(moon, sun):
    if not moon:
        return None
    phrase = _moon_phrase(moon)
    bright = moon["illuminated"] >= MOON_BRIGHT
    if moon["name"] == "new":
        if sun and sun["darkness"] == "night":
            return ("The sky was fully dark and the Moon was new: no "
                    "moonlight, no twilight, just stars.")
        return "The Moon was new, so it added no light to the sky."
    if moon["in_frame"] or moon["near_frame"]:
        where = "in the frame" if moon["in_frame"] else "just outside the frame"
        if bright:
            return (f"The Moon {where} was {phrase}, bright enough to wash "
                    "out the fainter stars.")
        return f"The Moon {where} was {phrase}."
    if moon["up"] is True:
        if bright:
            return (f"The Moon was {phrase} and up, which brightens the whole "
                    "sky and hides the fainter stars.")
        return f"The Moon was {phrase} and up, adding only a little light."
    if moon["up"] is False:
        if bright:
            return (f"The Moon was {phrase} but below the horizon, so it took "
                    "nothing from the sky.")
        return f"The Moon was {phrase} and below the horizon."
    # Altitude unknown: either no location was worked out, or the region
    # estimate straddles the horizon. Say so rather than stopping at the
    # phase — the narrator read that half-sentence as the Moon being
    # absent, a condition nothing here measured.
    return (f"The Moon was {phrase}, though there is no telling from this "
            "photo whether it had risen.")


def _depth_line(depth):
    if not depth or depth.get("limiting_mag") is None:
        return None
    mag = depth["limiting_mag"]
    words = next(w for limit, w in DEPTH_BANDS if mag < limit)
    # Catalog-limited means the stars kept showing to the end of the
    # catalog: a floor on the answer, not the answer.
    reach = "to at least" if depth.get("catalog_limited") else "down to"
    line = f"Stars {reach} magnitude {mag:.1f} show in this photo, {words}"
    exposure = _exposure_phrase(depth.get("exposure_seconds"))
    return line + (f", from a {exposure} exposure." if exposure else ".")


def describe(night, month=None):
    """The sentences for a night dict, in reading order. Pure, so the
    tests can pin every branch without an ephemeris."""
    lines = [_sun_line(night.get("sun"), month),
             _moon_line(night.get("moon"), night.get("sun")),
             _depth_line(night.get("depth"))]
    return [l for l in lines if l]


def _field_centre(wcs_path):
    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(wcs_path) as hdul:
        ra, dec = WCS(hdul[0].header).wcs.crval
    return float(ra), float(dec)


def annotate(exif_info, wcs_path, labels, pointers, verification):
    """The night dict: sun, moon, depth (each possibly None) and lines.
    Never fails a solve over a missing timestamp or ephemeris: whatever
    can't be computed is left out, and the lines say only what's known."""
    depth = None
    measured = (verification or {}).get("depth") or {}
    if measured.get("limiting_mag") is not None:
        depth = {"limiting_mag": measured["limiting_mag"],
                 "catalog_limited": bool(measured.get("catalog_limited")),
                 "exposure_seconds": exif_info.get("exposure_seconds")}

    night = {"time_utc": None, "location_source": None,
             "sun": None, "moon": None, "depth": depth, "lines": []}
    when_utc, _ = ephemeris.resolve_utc(exif_info)
    if when_utc is not None:
        try:
            eph = ephemeris.load_ephemeris()
        except FileNotFoundError:
            eph = None
        if eph is not None:
            ts = ephemeris._timescale()
            positions, source = observers(exif_info, _field_centre(wcs_path),
                                          when_utc)
            tracks = _sun_tracks(eph, ts, when_utc, positions)
            night["time_utc"] = when_utc.isoformat()
            night["location_source"] = source
            night["sun"] = _sun(tracks, fixed=(source == "gps"))
            night["moon"] = _moon(eph, ts, when_utc, [p for p, _, _ in tracks],
                                  labels, pointers)
    night["lines"] = describe(night, _local_month(exif_info))
    return night
