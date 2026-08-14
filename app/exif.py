"""Pull the EXIF fields we care about: field-of-view hints now, time/GPS for
the Phase 2 ephemeris layer."""

import math

from PIL import Image

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_FOCAL_LENGTH = 37386
TAG_FOCAL_35MM = 41989
TAG_DATETIME_ORIGINAL = 36867
TAG_OFFSET_TIME_ORIGINAL = 36881
TAG_EXPOSURE_TIME = 33434
TAG_GPS_IMG_DIRECTION_REF = 16  # 'M' magnetic / 'T' true
TAG_GPS_IMG_DIRECTION = 17

# Fallback when EXIF gives us nothing: generous phone-plausible field widths.
DEFAULT_FOV_BOUNDS = (30.0, 90.0)


def _gps_to_degrees(dms, ref):
    try:
        deg = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None
    if ref in ("S", "W"):
        deg = -deg
    return deg


def read_exif(path):
    """Return {fov_bounds, focal_35mm, datetime_original, offset_time_original,
    exposure_seconds, lat, lon, heading, heading_ref, width, height}."""
    info = {
        "fov_bounds": DEFAULT_FOV_BOUNDS,
        "focal_35mm": None,
        "datetime_original": None,
        "offset_time_original": None,
        "exposure_seconds": None,
        "lat": None,
        "lon": None,
        "heading": None,
        "heading_ref": None,
    }
    with Image.open(path) as img:
        info["width"], info["height"] = img.size
        ex = img.getexif()
        exif_ifd = ex.get_ifd(EXIF_IFD)
        gps = ex.get_ifd(GPS_IFD)

    f35 = exif_ifd.get(TAG_FOCAL_35MM)
    if f35:
        f35 = float(f35)
        info["focal_35mm"] = f35
        # Horizontal FOV for a 36mm-wide full frame at this equivalent focal
        # length. The bracket is deliberately lopsided: sensor crops and
        # digital zoom only ever make the real field NARROWER than the lens
        # implies, and nothing makes it wider, so this estimate is an upper
        # bound with a long tail below it.
        #
        # Measured 2026-08-14 on Pixel 9 astro shots: EXIF reports 24mm
        # equivalent (~74 deg) while the saved 4000x3000 frame is a 2x crop
        # of the 50MP sensor and truly spans 38.4 deg — a ratio of 0.52,
        # identical across every shot. The old symmetric 0.7-1.4 bracket
        # started at 52 deg, so the quick pass could not solve those photos
        # at all: it burned a full cpulimit before the fallback tier picked
        # them up on a "try harder" click. 0.35 covers a ~3x crop.
        fov = math.degrees(2 * math.atan(36.0 / (2 * f35)))
        info["fov_bounds"] = (max(1.0, fov * 0.35), min(180.0, fov * 1.2))

    dto = exif_ifd.get(TAG_DATETIME_ORIGINAL)
    if dto:
        info["datetime_original"] = str(dto)

    oto = exif_ifd.get(TAG_OFFSET_TIME_ORIGINAL)
    if oto:
        info["offset_time_original"] = str(oto)

    # Exposure time feeds the satellite-crossing window (#11): phone astro
    # modes record the per-frame value (e.g. 16s) as a rational.
    exp = exif_ifd.get(TAG_EXPOSURE_TIME)
    if exp is not None:
        try:
            exp = float(exp)
            if math.isfinite(exp) and 0 < exp < 3600:
                info["exposure_seconds"] = exp
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if gps:
        lat = _gps_to_degrees(gps.get(2), gps.get(1))
        lon = _gps_to_degrees(gps.get(4), gps.get(3))
        info["lat"], info["lon"] = lat, lon

        # Compass heading at capture: phones record this even when they drop
        # lat/lon, and it's what makes the no-solve fallback (#7) possible.
        direction = gps.get(TAG_GPS_IMG_DIRECTION)
        if direction is not None:
            try:
                heading = float(direction)
            except (TypeError, ValueError, ZeroDivisionError):
                heading = None
            if heading is not None and math.isfinite(heading):
                info["heading"] = heading % 360.0
                ref = gps.get(TAG_GPS_IMG_DIRECTION_REF)
                info["heading_ref"] = (str(ref).strip() or None) if ref else None

    return info


def strip_gps(path):
    """Remove the GPS IFD from a JPEG in place, losslessly (segment surgery,
    pixels untouched). Called after read_exif() has captured precise
    coordinates into the job record — the stored file is served publicly
    (#22), so it must not carry the photographer's location. Raises on
    non-JPEG input; callers treat this as best-effort."""
    import piexif

    exif_dict = piexif.load(path)
    if not exif_dict.get("GPS"):
        return False
    exif_dict["GPS"] = {}
    piexif.insert(piexif.dump(exif_dict), path)
    return True
