"""Synthetic test images: star fields rendered from the HYG catalog with a
known WCS (exact ground truth for solver tests), plus obviously-unsolvable
images (noise, black, gradient) for graceful-failure tests."""

import csv
import os

import numpy as np
from PIL import Image
from astropy.wcs import WCS

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_FOCAL_35MM = 41989
TAG_DATETIME_ORIGINAL = 36867


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


def build_exif(f35mm=None, datetime_original=None, gps=None, heading=None):
    """PIL Exif with the fields app.exif reads. gps = (lat, lon) in degrees;
    heading = (degrees, ref) with ref 'M' (magnetic) or 'T' (true)."""
    ex = Image.Exif()
    ifd = ex.get_ifd(EXIF_IFD)
    if f35mm is not None:
        ifd[TAG_FOCAL_35MM] = int(f35mm)
    if datetime_original is not None:
        ifd[TAG_DATETIME_ORIGINAL] = datetime_original
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


def stamp_stars(arr, stars):
    """Add Gaussian star stamps to a float image array in place.
    stars = [(x, y, amp), ...] in pixel coordinates."""
    height, width = arr.shape
    sigma = 1.3
    r = 5
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


def render_points(path, points, width=1200, height=900, amp=180.0, seed=11,
                  amps=None, blobs=None):
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
    stamp_stars(arr, [(x, y, a) for (x, y), a in zip(points, amps)])
    if blobs:
        stamp_blobs(arr, blobs)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB")
    img.save(path, quality=92)


def render_starfield(path, ra=95.0, dec=-10.0, fov_deg=50.0, width=1600,
                     height=1200, max_mag=5.5, seed=42, f35mm=39):
    """Render a star field and save it as JPEG with a matching EXIF focal
    length. Returns the WCS used, i.e. the ground truth."""
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

    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB")
    img.save(path, quality=92, exif=build_exif(f35mm=f35mm))
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
