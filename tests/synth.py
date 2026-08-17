"""Synthetic test images: star fields rendered from the HYG catalog with a
known WCS (exact ground truth for solver tests), plus obviously-unsolvable
images (noise, black, gradient) for graceful-failure tests."""

import csv
import math
import os
from fractions import Fraction

import numpy as np
from PIL import Image
from astropy.wcs import WCS

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")

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


def load_stars(max_mag):
    """(ra_deg, dec_deg, mag) rows from the HYG catalog, no name filter."""
    path = os.path.join(CATALOG_DIR, "hyg.csv")
    stars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                mag = float(row["mag"])
                ra = float(row["ra"]) * 15.0  # HYG stores RA in hours
                dec = float(row["dec"])
            except (KeyError, ValueError):
                continue
            if mag <= max_mag:
                stars.append((ra, dec, mag))
    return stars


def make_wcs(ra, dec, fov_deg, width, height):
    """Gnomonic (TAN) WCS centered on (ra, dec) spanning fov_deg horizontally."""
    scale = fov_deg / width
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [width / 2, height / 2]
    w.wcs.cd = [[-scale, 0.0], [0.0, scale]]
    return w


def build_exif(f35mm=None, datetime_original=None, gps=None, heading=None,
               exposure_seconds=None, focal_length=None,
               focal_plane_x_res=None, focal_plane_unit=None,
               pixel_x_dimension=None, offset_time_original=None):
    """PIL Exif with the fields app.exif reads. gps = (lat, lon) in degrees;
    heading = (degrees, ref) with ref 'M' (magnetic) or 'T' (true).

    focal_length + focal_plane_x_res/unit stand in for a dedicated camera that
    records no 35mm equivalent (#70)."""
    ex = Image.Exif()
    ifd = ex.get_ifd(EXIF_IFD)
    if f35mm is not None:
        ifd[TAG_FOCAL_35MM] = int(f35mm)
    if focal_length is not None:
        ifd[TAG_FOCAL_LENGTH] = focal_length
    if focal_plane_x_res is not None:
        ifd[TAG_FOCAL_PLANE_X_RESOLUTION] = focal_plane_x_res
    if focal_plane_unit is not None:
        ifd[TAG_FOCAL_PLANE_RESOLUTION_UNIT] = focal_plane_unit
    if pixel_x_dimension is not None:
        ifd[TAG_PIXEL_X_DIMENSION] = pixel_x_dimension
    if datetime_original is not None:
        ifd[TAG_DATETIME_ORIGINAL] = datetime_original
    if offset_time_original is not None:
        # DateTimeOriginal is local time with no zone; without this tag the
        # instant is only as good as a zone lookup. Satellites care: a low
        # pass moves about a degree a second.
        ifd[TAG_OFFSET_TIME_ORIGINAL] = offset_time_original
    if exposure_seconds is not None:
        # A rational, as a real camera writes it. A bare float round-trips
        # through Pillow but is not valid EXIF, and piexif refuses to
        # re-encode it — which used to reject the upload outright.
        if isinstance(exposure_seconds, tuple):
            ifd[TAG_EXPOSURE_TIME] = exposure_seconds
        else:
            f = Fraction(exposure_seconds).limit_denominator(100000)
            ifd[TAG_EXPOSURE_TIME] = (f.numerator, f.denominator)
    if gps is not None or heading is not None:
        g = ex.get_ifd(GPS_IFD)
    if gps is not None:
        lat, lon = gps
        g[1] = "N" if lat >= 0 else "S"
        g[2] = _deg_to_dms(abs(lat))
        g[3] = "E" if lon >= 0 else "W"
        g[4] = _deg_to_dms(abs(lon))
    if heading is not None:
        deg, ref = heading
        g[16] = ref
        g[17] = deg
    return ex


def _deg_to_dms(deg):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round((deg - d - m / 60) * 3600, 2)
    return (d, m, s)


def stamp_stars(arr, stars, sigma=1.3):
    """Add Gaussian star stamps to a float image array in place.
    stars = [(x, y, amp), ...] in pixel coordinates. `sigma` widens the
    PSF — phone night modes stack into fat stars, which is what makes
    scale matter for the pre-solve gate."""
    height, width = arr.shape
    r = max(5, int(round(3 * sigma)))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    stamp = np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2))
    for x, y, amp in stars:
        xi, yi = int(round(x)), int(round(y))
        x0, x1 = max(0, xi - r), min(width, xi + r + 1)
        y0, y1 = max(0, yi - r), min(height, yi + r + 1)
        arr[y0:y1, x0:x1] += amp * stamp[
            y0 - (yi - r):stamp.shape[0] - ((yi + r + 1) - y1),
            x0 - (xi - r):stamp.shape[1] - ((xi + r + 1) - x1),
        ]


def stamp_blobs(arr, blobs):
    """Add wide Gaussian blobs (diffuse DSO stand-ins) to a float image
    array in place. blobs = [(x, y, amp, sigma)] in pixel coordinates."""
    height, width = arr.shape
    yy, xx = np.mgrid[0:height, 0:width]
    for x, y, amp, sigma in blobs:
        arr += amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2))


def stamp_trail(arr, points, amp=200.0, sigma=1.3, step=0.5):
    """Draw a continuous streak through consecutive (x, y) points, in place.

    What a satellite actually leaves on a long exposure: the same PSF as a
    star, smeared along its apparent motion. Sampling the polyline at
    sub-pixel spacing and stamping the star PSF at each sample gives a line
    of the right width and brightness profile, without a separate
    line-drawing path that could disagree with how stars are rendered.
    """
    samples = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        span = math.hypot(x1 - x0, y1 - y0)
        for i in range(max(1, int(span / step))):
            f = i / max(1, int(span / step))
            samples.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, amp * step))
    if points:
        samples.append((points[-1][0], points[-1][1], amp * step))
    stamp_stars(arr, samples, sigma=sigma)


def render_points(path, points, width=1200, height=900, amp=180.0, seed=11,
                  amps=None, blobs=None, sigma=1.3):
    """Render Gaussian stars at explicit pixel positions over a flat noisy
    background — ground truth for verification tests, no WCS involved.
    `amps` overrides the uniform amplitude per point; `blobs` adds diffuse
    [(x, y, amp, sigma)] Gaussians (synthetic nebulae/galaxies)."""
    rng = np.random.default_rng(seed)
    arr = np.full((height, width), 10.0)
    arr += rng.normal(0.0, 2.0, arr.shape)
    if amps is None:
        amps = [amp] * len(points)
    if len(amps) != len(points):
        raise ValueError(f"amps has {len(amps)} entries for {len(points)} points")
    stamp_stars(arr, [(x, y, a) for (x, y), a in zip(points, amps)], sigma=sigma)
    if blobs:
        stamp_blobs(arr, blobs)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB")
    img.save(path, quality=92)


def render_starfield(path, ra=95.0, dec=-10.0, fov_deg=50.0, width=1600,
                     height=1200, max_mag=5.5, seed=42, f35mm=39,
                     trails=None, exif=None):
    """Render a star field and save it as JPEG with a matching EXIF focal
    length. Returns the WCS used, i.e. the ground truth.

    `trails` are [[(ra, dec), ...], ...] polylines in sky coordinates,
    projected through the same WCS and drawn as streaks — a satellite
    crossing the frame during the exposure (#11). `exif` overrides the
    default focal-length-only block when a test needs GPS, a timestamp or
    an exposure time in the file.
    """
    wcs = make_wcs(ra, dec, fov_deg, width, height)
    stars = load_stars(max_mag)
    ras = np.array([s[0] for s in stars])
    decs = np.array([s[1] for s in stars])
    mags = np.array([s[2] for s in stars])
    xs, ys = wcs.all_world2pix(ras, decs, 0)

    rng = np.random.default_rng(seed)
    arr = np.full((height, width), 10.0)
    arr += rng.normal(0.0, 2.0, arr.shape)

    # TAN projection sends the antipodal hemisphere to wild pixel values;
    # keep only stars that genuinely land on the frame.
    r = 5
    ok = np.isfinite(xs) & np.isfinite(ys) & \
        (xs > -r) & (xs < width + r) & (ys > -r) & (ys < height + r)
    stamp_stars(arr, [(x, y, 255.0 * 10 ** (-0.4 * (mag - 3.0)))
                      for x, y, mag in zip(xs[ok], ys[ok], mags[ok])])

    for trail in (trails or []):
        tx, ty = wcs.all_world2pix([p[0] for p in trail], [p[1] for p in trail], 0)
        stamp_trail(arr, list(zip(tx, ty)))

    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB")
    img.save(path, quality=92,
             exif=exif if exif is not None else build_exif(f35mm=f35mm))
    return wcs


def render_noise(path, width=1600, height=1200, seed=7):
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, (height, width), dtype=np.uint8)
    Image.fromarray(arr).convert("RGB").save(path, quality=92)


def render_black(path, width=1600, height=1200):
    Image.new("RGB", (width, height)).save(path, quality=92)


def render_gradient(path, width=1600, height=1200):
    """A smooth daylight-ish gradient: plenty of signal, zero stars."""
    col = np.linspace(60, 220, height, dtype=np.uint8)
    arr = np.repeat(col[:, None], width, axis=1)
    Image.fromarray(arr).convert("RGB").save(path, quality=92)
