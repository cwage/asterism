"""Pull the EXIF fields we care about: field-of-view hints now, time/GPS for
the Phase 2 ephemeris layer."""

import math

from PIL import Image

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_FOCAL_LENGTH = 37386
TAG_FOCAL_35MM = 41989
TAG_DATETIME_ORIGINAL = 36867

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
    """Return {fov_bounds, focal_35mm, datetime_original, lat, lon, width, height}."""
    info = {
        "fov_bounds": DEFAULT_FOV_BOUNDS,
        "focal_35mm": None,
        "datetime_original": None,
        "lat": None,
        "lon": None,
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
        # length; bracket it to absorb crop/pano/EXIF weirdness.
        fov = math.degrees(2 * math.atan(36.0 / (2 * f35)))
        info["fov_bounds"] = (max(1.0, fov * 0.7), min(180.0, fov * 1.4))

    dto = exif_ifd.get(TAG_DATETIME_ORIGINAL)
    if dto:
        info["datetime_original"] = str(dto)

    if gps:
        lat = _gps_to_degrees(gps.get(2), gps.get(1))
        lon = _gps_to_degrees(gps.get(4), gps.get(3))
        info["lat"], info["lon"] = lat, lon

    return info
