"""Pull the EXIF fields we care about: field-of-view hints now, time/GPS for
the Phase 2 ephemeris layer."""

import math

from PIL import Image

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_FOCAL_LENGTH = 37386
TAG_FOCAL_35MM = 41989
TAG_FOCAL_PLANE_X_RESOLUTION = 41486
TAG_FOCAL_PLANE_RESOLUTION_UNIT = 41488
TAG_PIXEL_X_DIMENSION = 40962
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


def _fov_bounds(f35):
    """Scale bracket around a 35mm-equivalent focal length.

    Horizontal FOV for a 36mm-wide full frame at this equivalent focal
    length. The bracket is deliberately lopsided: sensor crops and digital
    zoom only ever make the real field NARROWER than the lens implies, and
    nothing makes it wider, so this estimate is an upper bound with a long
    tail below it.

    Measured 2026-08-14 on Pixel 9 astro shots: EXIF reports 24mm equivalent
    (~74 deg) while the saved 4000x3000 frame is a 2x crop of the 50MP sensor
    and truly spans 38.4 deg — a ratio of 0.52, identical across every shot.
    The old symmetric 0.7-1.4 bracket started at 52 deg, so the quick pass
    could not solve those photos at all: it burned a full cpulimit before the
    fallback tier picked them up on a "try harder" click. 0.35 covers a ~3x
    crop.

    The same bracket covers a focal length derived from sensor width: a
    dedicated camera is less likely to be secretly cropping, but an exported
    crop keeps the focal length and narrows the field exactly the same way."""
    fov = math.degrees(2 * math.atan(36.0 / (2 * f35)))
    return (max(1.0, fov * 0.35), min(180.0, fov * 1.2))


# EXIF resolution units. 2/3 are the standard inch/cm; 4/5 appear in the wild.
_RESOLUTION_UNIT_MM = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}

# A recorded frame narrower than Super 16 or wider than medium format means the
# arithmetic went wrong — most likely the file was resized without the focal
# plane tags being updated, so the pixel width no longer matches them.
MIN_SENSOR_MM = 4.0
MAX_SENSOR_MM = 70.0


def _derive_focal_35mm(exif_ifd, px_width):
    """35mm equivalent from FocalLength plus the focal plane tags, or None.

    FocalPlaneXResolution is the recorded image's pixel density across the
    sensor, so width_px / density gives the width actually captured, and
    36mm / that width is the crop factor.

    Cropping is handled correctly for free: cutting pixels away leaves the
    density untouched, so the arithmetic returns the smaller sensor extent
    that was actually kept. Resizing is the opposite — it rewrites the pixel
    count while the density still describes the original capture — so a file
    whose stored dimensions disagree with its actual ones is refused."""
    focal = exif_ifd.get(TAG_FOCAL_LENGTH)
    density = exif_ifd.get(TAG_FOCAL_PLANE_X_RESOLUTION)
    unit_mm = _RESOLUTION_UNIT_MM.get(exif_ifd.get(TAG_FOCAL_PLANE_RESOLUTION_UNIT))
    if not focal or not density or not unit_mm or not px_width:
        return None

    # PixelXDimension is what the camera recorded. A resizer that updates it
    # and leaves the density stale is caught here; one that updates neither
    # falls to the sensor-width sanity check below.
    stored_width = exif_ifd.get(TAG_PIXEL_X_DIMENSION)
    if stored_width and int(stored_width) != int(px_width):
        return None
    try:
        sensor_mm = px_width / float(density) * unit_mm
        focal = float(focal)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not (MIN_SENSOR_MM <= sensor_mm <= MAX_SENSOR_MM) or focal <= 0:
        return None
    return focal * 36.0 / sensor_mm


def read_exif(path):
    """Return {fov_bounds, focal_35mm, datetime_original, offset_time_original,
    exposure_seconds, lat, lon, heading, heading_ref, width, height}."""
    info = {
        "fov_bounds": DEFAULT_FOV_BOUNDS,
        "focal_35mm": None,
        # Which route produced focal_35mm: the EXIF tag, or the sensor-width
        # derivation (#70). Recorded so a bad scale hint is traceable.
        "focal_35mm_source": None,
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
        info["focal_35mm"] = float(f35)
        info["focal_35mm_source"] = "exif_35mm"
    else:
        # No 35mm-equivalent tag. Dedicated cameras often record only the real
        # focal length, plus enough to work out the sensor width (#70): the
        # Canon 5DS upload that started this reported 24mm with no equivalent,
        # so it fell to the generic tiers and burned 315s on a field its own
        # EXIF put at ~74 deg.
        derived = _derive_focal_35mm(exif_ifd, info["width"])
        if derived:
            info["focal_35mm"] = derived
            info["focal_35mm_source"] = "sensor_width"

    if info["focal_35mm"]:
        info["fov_bounds"] = _fov_bounds(info["focal_35mm"])

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
