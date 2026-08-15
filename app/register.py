"""Register a frame from the Moon and planets when there aren't enough stars
to plate-solve (#85).

A twilight or light-polluted frame can hold nothing a star matcher can use and
still contain the two most recognisable objects in the sky. Their positions at
the exposure instant are known to arcseconds, so two of them identified in the
pixels pin down pointing, roll and plate scale — a complete WCS, after which
every existing label layer works unchanged.

STATUS: the geometry works and is tested; identification does not. Nothing
here is wired into the worker, and it should not be until that changes.

`wcs_from_pair` recovers a known WCS to under a pixel across the frame, and on
a real dusk photo it turned Moon + Venus into a 39.3 degree field whose centre
landed at azimuth 263 — a westward frame holding both, which nothing in the fit
knew about.

Deciding *which* blob is the Moon is the unsolved half. The checks below (sky
mask, field bracket, lunar size, and a crescent whose lit limb must face the
Sun) take a real frame from 119 detected sources down to one correct answer.
They then get it wrong on four sibling frames from the same walk, with limb
agreement of 0.0-2.6 degrees and hundreds of surviving alternatives: the
thresholds are fitted to the one frame they were developed against. A ground
light under a tree was confidently labelled the Moon.

That failure mode — a plausible, unverifiable, confidently wrong answer — is
the one #71 exists to prevent, so this stays out of the pipeline until
identification is either confirmed by the user (tap the Moon) or constrained by
a third body that can check the other two.
"""

import math

import numpy as np

# The Moon's apparent diameter varies 29.4-33.5 arcmin over its orbit; half a
# degree is close enough to size a detection window.
MOON_DIAMETER_DEG = 0.52

# Two sources closer than this on the sky can't be told apart reliably by a
# separation match alone, so a pair has to be wider to be usable.
MIN_PAIR_SEPARATION_DEG = 2.0


def angular_separation(ra1, dec1, ra2, dec2):
    """Great-circle angle between two sky positions, in degrees."""
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    cos_d = (math.sin(dec1) * math.sin(dec2)
             + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


def _unit(ra, dec):
    ra, dec = math.radians(ra), math.radians(dec)
    return np.array([math.cos(dec) * math.cos(ra),
                     math.cos(dec) * math.sin(ra),
                     math.sin(dec)])


def _radec(vec):
    x, y, z = vec / np.linalg.norm(vec)
    return math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.asin(z))


def _midpoint(ra1, dec1, ra2, dec2):
    """Spherical midpoint of two sky positions."""
    return _radec(_unit(ra1, dec1) + _unit(ra2, dec2))


def _position_angle(ra1, dec1, ra2, dec2):
    """Bearing of point 2 seen from point 1, degrees east of north."""
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    dra = ra2 - ra1
    y = math.sin(dra) * math.cos(dec2)
    x = (math.cos(dec1) * math.sin(dec2)
         - math.sin(dec1) * math.cos(dec2) * math.cos(dra))
    return math.degrees(math.atan2(y, x)) % 360.0


def wcs_from_pair(pix1, sky1, pix2, sky2, width, height):
    """A TAN WCS from two identified objects, or None if they're unusable.

    pix are (x, y) in pixels; sky are (ra, dec) in degrees. Exactly determined:
    four unknowns (reference point, roll, scale) against four measurements, so
    the fit is a construction rather than a least-squares problem — which is
    also why it cannot detect its own errors. Callers must verify.
    """
    from astropy.wcs import WCS

    (x1, y1), (x2, y2) = pix1, pix2
    (ra1, dec1), (ra2, dec2) = sky1, sky2

    pixel_sep = math.hypot(x2 - x1, y2 - y1)
    sky_sep = angular_separation(ra1, dec1, ra2, dec2)
    if pixel_sep < 1.0 or sky_sep < MIN_PAIR_SEPARATION_DEG:
        return None

    # Reference the projection at the pair's midpoint, where the gnomonic
    # distortion over the baseline is symmetric and smallest.
    ra0, dec0 = _midpoint(ra1, dec1, ra2, dec2)

    # TAN maps an angle rho from the reference to tan(rho) on the tangent
    # plane, so the scale is set by the half-separation, not the whole one.
    # Over a 16 degree baseline the difference is ~0.3% — small, but it is a
    # systematic that would otherwise sit in every label position.
    half_plane_deg = math.degrees(math.tan(math.radians(sky_sep / 2.0)))
    scale = half_plane_deg / (pixel_sep / 2.0)

    # Roll: the sky bearing of object 2 from the midpoint, against the same
    # bearing measured in pixels.
    #
    # With the CD matrix below, a pixel displacement (dx, dy) has intermediate
    # coordinates xi = s*r*sin(beta + roll), eta = s*r*cos(beta + roll) where
    # beta = atan2(-dx, dy). Position angle is atan2(xi, eta), so it is simply
    # beta + roll — hence roll = PA_sky - beta. The minus sign on dx is the
    # parity flip every sky image has (east is left with north up); dropping it
    # puts every label 180 degrees out, which is exactly the whole baseline.
    pa_sky = _position_angle(ra0, dec0, ra2, dec2)
    beta = math.degrees(math.atan2(-(x2 - x1), y2 - y1)) % 360.0
    roll = math.radians(pa_sky - beta)

    # Seed a WCS tangent at the pair midpoint. This is only a starting guess:
    # a lens is gnomonic about its optical axis, so a TAN projection tangent
    # anywhere else describes a different camera. Anchored on the pair it
    # reproduces the two objects exactly and drifts everywhere else — 246 px at
    # the frame edge on a 70 degree field.
    seed = _build(ra0, dec0, math.degrees(roll), scale,
                  (x1 + x2) / 2.0, (y1 + y2) / 2.0, width, height)

    # Move the tangent point to the frame centre, where the optical axis is,
    # and refine. Four unknowns (reference position, roll, scale) against the
    # four measurements, so this is still exactly determined — it just has no
    # closed form once the reference is pinned to the centre.
    ra_c, dec_c = seed.all_pix2world(width / 2.0, height / 2.0, 0)
    guess = [float(ra_c), float(dec_c), math.degrees(roll), scale]
    return _refine(guess, pix1, sky1, pix2, sky2, width, height)


def _build(ra0, dec0, roll_deg, scale, crpix_x, crpix_y, width, height):
    from astropy.wcs import WCS

    roll = math.radians(roll_deg)
    cos_r, sin_r = math.cos(roll), math.sin(roll)
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [ra0, dec0]
    # crpix is 1-based in FITS; every pixel coordinate here is 0-based.
    wcs.wcs.crpix = [crpix_x + 1.0, crpix_y + 1.0]
    wcs.wcs.cd = [[-scale * cos_r, scale * sin_r],
                  [scale * sin_r, scale * cos_r]]
    wcs.pixel_shape = (width, height)
    return wcs


def _refine(params, pix1, sky1, pix2, sky2, width, height, iterations=12):
    """Gauss-Newton on (ra, dec, roll, scale) with the tangent point at the
    frame centre. Returns the fitted WCS, or None if it fails to converge."""
    targets = np.array([pix1[0], pix1[1], pix2[0], pix2[1]], dtype=float)
    sky = (sky1, sky2)

    def residual(p):
        # Nonsense parameters are normal here: identification sweeps thousands
        # of wrong pairings, and the inverse projection can simply refuse to
        # converge for some of them. That is a rejected candidate, not an error.
        wcs = _build(p[0], p[1], p[2], p[3], width / 2.0, height / 2.0,
                     width, height)
        got = []
        for ra, dec in sky:
            px, py = wcs.all_world2pix(ra, dec, 0)
            got += [float(px), float(py)]
        return np.array(got) - targets, wcs

    if not all(np.isfinite(params)):
        return None
    p = np.array(params, dtype=float)
    # Step ra in proportion to cos(dec) so the parameterisation stays roughly
    # isotropic; it degenerates at the poles, which no phone photo reaches by
    # accident but which is worth knowing about.
    steps = np.array([1e-4 / max(0.05, math.cos(math.radians(p[1]))),
                      1e-4, 1e-3, p[3] * 1e-4])
    try:
        for _ in range(iterations):
            r, wcs = residual(p)
            if np.max(np.abs(r)) < 1e-3:
                return wcs
            jac = np.empty((4, 4))
            for k in range(4):
                q = p.copy()
                q[k] += steps[k]
                jac[:, k] = (residual(q)[0] - r) / steps[k]
            p = p - np.linalg.solve(jac, r)
            if not np.all(np.isfinite(p)):
                return None
        r, wcs = residual(p)
    except Exception:
        return None
    return wcs if np.max(np.abs(r)) < 0.5 else None


def pair_residuals(wcs, matches):
    """Pixel distances between where each body was measured and where the
    WCS puts it. matches = [((x, y), (ra, dec)), ...]."""
    out = []
    for (x, y), (ra, dec) in matches:
        px, py = wcs.all_world2pix(ra, dec, 0)
        out.append(math.hypot(float(px) - x, float(py) - y))
    return out


# --- finding the bright things ---------------------------------------------

# Background scale. Must be comfortably larger than anything we want to keep,
# or the object contributes to its own background and gets subtracted: a box
# blur of radius 40 measurably eats a 50 px Moon.
BACKGROUND_BLOCK = 128

# Foliage against a bright twilight sky is the dominant false source. A dark
# silhouette drags the local background down and makes the sky beside it read
# as a strong positive, so edges outnumbered real sources 400:1 in the first
# detector tried against a real dusk frame. Real sky objects sit on smooth
# sky; branch edges sit on texture, which is what this rejects.
MAX_ANNULUS_TEXTURE = 6.0


def _block_background(a, block=BACKGROUND_BLOCK):
    """Median background on a coarse grid, upsampled. Median rather than mean
    because tree silhouettes are extreme dark outliers."""
    h, w = a.shape
    bh, bw = max(1, h // block), max(1, w // block)
    trimmed = a[:bh * block, :bw * block]
    grid = np.median(trimmed.reshape(bh, block, bw, block), axis=(1, 3))
    bg = np.repeat(np.repeat(grid, block, 0), block, 1)
    if bg.shape != a.shape:                       # edge remainder
        bg = np.pad(bg, ((0, h - bg.shape[0]), (0, w - bg.shape[1])), mode="edge")
    return bg


def _downsample(a, f):
    """Block-mean by an integer factor — cheap, and it suppresses the noise
    that would otherwise dominate a local-maximum search."""
    h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    return a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def _local_maxima(a, r=2):
    """Boolean mask of pixels that are the maximum of their neighbourhood,
    via two separable sliding-window passes."""
    from numpy.lib.stride_tricks import sliding_window_view

    pad = np.pad(a, r, mode="edge")
    rows = sliding_window_view(pad, 2 * r + 1, axis=1).max(axis=-1)
    both = sliding_window_view(rows, 2 * r + 1, axis=0).max(axis=-1)
    return a >= both[: a.shape[0], : a.shape[1]]


def detect_sources(image_path, max_sources=150, threshold_sigma=8.0, factor=4):
    """Compact bright sources, brightest first.

    Returns dicts of x, y, peak, extent (width at half maximum, in full-frame
    pixels) — enough to tell an extended Moon from a point-like planet.

    Detection runs on a downsampled copy. A dusk frame puts over a million
    pixels above any useful threshold — lit foliage, windows, streetlights —
    so ranking raw pixels by brightness never reaches a Moon sitting at 115
    counts behind a porch light at 249. Local maxima cut that to hundreds.
    """
    from PIL import Image

    with Image.open(image_path) as im:
        full = np.asarray(im.convert("L"), dtype=np.float32)
    small = _downsample(full, factor)
    det = small - _block_background(small, max(8, BACKGROUND_BLOCK // factor))
    sigma = float(1.4826 * np.median(np.abs(det - np.median(det)))) or 1.0
    thr = max(threshold_sigma * sigma, 4.0)

    peaks = _local_maxima(det) & (det > thr)
    ys, xs = np.where(peaks)
    if len(xs) == 0:
        return []
    # Every candidate, not the brightest few: on a real dusk frame the Moon
    # ranked 1824th and Venus 468th of 4110 maxima, behind every lit window and
    # porch light in the street. Brightness is the wrong prior for "is this a
    # sky object", so the detector stays permissive and identification does the
    # discriminating.
    order = np.argsort(det[ys, xs])[::-1]

    h, w = det.shape
    found = []
    for i in order:
        y, x = int(ys[i]), int(xs[i])
        # Compare against where each kept source was *detected*, not where its
        # centroid was later refined to: mixing the two let one tree-edge blob
        # register five times, 30 px apart, and crowd out the real objects.
        if any((x * factor - s["raw_x"]) ** 2 + (y * factor - s["raw_y"]) ** 2
               < max(40, s["extent"]) ** 2 for s in found):
            continue
        # Size the source first, then judge its surroundings from a window
        # scaled to it. A fixed window is all core for anything wide: a
        # Moon-sized blob measured its own bright wings as "texture" and threw
        # itself out.
        core_rad = _core_radius(det, x, y)
        extent = int((2 * core_rad + 1) * factor)
        if extent > 90:                           # a ridge or a wall, not a source
            continue
        rad = int(max(12, core_rad * 2 + 8))
        y0, y1 = max(0, y - rad), min(h, y + rad + 1)
        x0, x1 = max(0, x - rad), min(w, x + rad + 1)
        patch = det[y0:y1, x0:x1]
        gy, gy_ = np.ogrid[y0 - y:y1 - y, x0 - x:x1 - x]
        dist = np.hypot(gy, gy_)
        ring = patch[(dist > core_rad * 1.4 + 2) & (dist <= core_rad * 1.4 + 10)]
        texture = float(ring.std()) / sigma if ring.size > 8 else 99.0
        if texture > MAX_ANNULUS_TEXTURE:
            continue
        # Centroid at full resolution: detection can be coarse, astrometry
        # cannot.
        cx, cy = _refine_centroid(full, x * factor + factor // 2,
                                  y * factor + factor // 2, extent)
        found.append({"x": cx, "y": cy, "peak": float(patch.max()),
                      "extent": extent, "texture": round(texture, 2),
                      "raw_x": x * factor, "raw_y": y * factor})
        if len(found) >= max_sources:
            break
    return found


def _core_radius(det, x, y, limit=24):
    """Radius at which the source falls to half its peak, in detection pixels."""
    peak = det[y, x]
    if peak <= 0:
        return 1
    for r in range(1, limit):
        y0, y1 = max(0, y - r), min(det.shape[0], y + r + 1)
        x0, x1 = max(0, x - r), min(det.shape[1], x + r + 1)
        edge = np.concatenate([det[y0, x0:x1], det[y1 - 1, x0:x1],
                               det[y0:y1, x0], det[y0:y1, x1 - 1]])
        if float(edge.max()) < peak / 2.0:
            return r
    return limit


def _refine_centroid(full, x, y, extent):
    """Brightness-weighted centroid in a window sized to the source."""
    half = max(12, int(extent))
    y0, y1 = max(0, int(y) - half), min(full.shape[0], int(y) + half + 1)
    x0, x1 = max(0, int(x) - half), min(full.shape[1], int(x) + half + 1)
    win = full[y0:y1, x0:x1]
    det = win - np.percentile(win, 40)
    det[det < det.max() * 0.35] = 0.0
    yy, xx = np.nonzero(det)
    if len(xx) == 0:
        return float(x), float(y)
    vals = det[yy, xx]
    return (x0 + float((xx * vals).sum() / vals.sum()),
            y0 + float((yy * vals).sum() / vals.sum()))


# --- identification ---------------------------------------------------------

# How far the Moon's measured width may sit from the width the candidate scale
# implies. A crescent measures narrower than the full disc it belongs to, and
# a detector's half-maximum width is not a precise diameter, so this is loose —
# but it is the constraint that makes two points self-checking. The scale
# derived from a pair's separation predicts a lunar size; the Moon's actual
# size predicts a scale. Requiring the two to agree is a genuine second
# measurement, not a restatement of the first.
MOON_EXTENT_RATIO = (0.7, 1.45)

# Plate scales outside this are not phone photographs of the sky.
FIELD_LIMITS_DEG = (1.0, 180.0)


def _candidate_pairs(bodies):
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            sep = angular_separation(bodies[i]["ra"], bodies[i]["dec"],
                                     bodies[j]["ra"], bodies[j]["dec"])
            if sep >= MIN_PAIR_SEPARATION_DEG:
                yield bodies[i], bodies[j], sep


def match_sources_to_bodies(sources, bodies, width, height, fov_bounds=None):
    """Every (source pair -> body pair) assignment that survives the checks,
    best first.

    The checks are what stand between this and a confident wrong answer, since
    a two-point fit reproduces its own anchors no matter how wrong they are.
    """
    low, high = fov_bounds or FIELD_LIMITS_DEG
    low = max(low, FIELD_LIMITS_DEG[0])
    high = min(high, FIELD_LIMITS_DEG[1])
    out = []

    for b1, b2, sep in _candidate_pairs(bodies):
        for i in range(len(sources)):
            for j in range(len(sources)):
                if i == j:
                    continue
                s1, s2 = sources[i], sources[j]
                d = math.hypot(s2["x"] - s1["x"], s2["y"] - s1["y"])
                if d < 20.0:
                    continue
                scale = sep / d                       # degrees per pixel
                if not (low <= scale * width <= high):
                    continue
                moon_ok, moon_score = _moon_consistent([(s1, b1), (s2, b2)], scale)
                if not moon_ok:
                    continue
                wcs = wcs_from_pair((s1["x"], s1["y"]), (b1["ra"], b1["dec"]),
                                    (s2["x"], s2["y"]), (b2["ra"], b2["dec"]),
                                    width, height)
                if wcs is None:
                    continue
                out.append({"wcs": wcs, "bodies": [b1["name"], b2["name"]],
                            "sources": [s1, s2], "field_deg": scale * width,
                            "moon_score": moon_score,
                            "peak_sum": s1["peak"] + s2["peak"]})
    # A Moon whose size matches the derived scale is the strongest signal
    # available; brightness only breaks ties.
    out.sort(key=lambda m: (m["moon_score"], -m["peak_sum"]))
    return out


def _moon_consistent(assignments, scale):
    """Is the source assigned to the Moon the right size for this scale?

    Returns (ok, score) where score is |log ratio| — zero when the measured
    width is exactly what the scale implies.
    """
    best = 0.0
    for source, body in assignments:
        if body.get("kind") != "moon":
            continue
        expected = MOON_DIAMETER_DEG / scale
        if expected <= 0:
            return False, 0.0
        ratio = source["extent"] / expected
        if not (MOON_EXTENT_RATIO[0] <= ratio <= MOON_EXTENT_RATIO[1]):
            return False, 0.0
        best = max(best, abs(math.log(ratio)))
    return True, best


# --- telling sky from street ------------------------------------------------

# Most bright things in a phone night photo are on the ground: windows, porch
# lights, streetlamps, lit foliage. On the real dusk frame 119 sources were
# detected and only a handful were in the sky, so pairing without this check
# left 4069 surviving assignments — the sky/ground split is what makes
# identification tractable at all.
SKY_TEXTURE_BLOCK = 32


def sky_mask(image_path, factor=4, block=SKY_TEXTURE_BLOCK):
    """Per-column height of the skyline, in full-frame pixels.

    Sky is smooth; trees, roofs and fences are not. Scanning down each column
    for the first sustained run of texture finds the horizon well enough to
    ask "was this source in the sky".
    """
    from PIL import Image

    with Image.open(image_path) as im:
        img = np.asarray(im.convert("L"), dtype=np.float32)
    small = _downsample(img, factor)
    h, w = (small.shape[0] // block) * block, (small.shape[1] // block) * block
    tiles = small[:h, :w].reshape(h // block, block, w // block, block)
    texture = tiles.std(axis=(1, 3))
    quiet = float(np.median(texture))
    rough = texture > max(3.0 * quiet, quiet + 2.0)

    # Find the step from sky to ground, rather than the first rough tile or
    # the last one. Scanning down, an isolated bright Moon is the first
    # "texture" and marks itself as the horizon; scanning up, a smooth wall or
    # road ends the ground early and lets every porch light back in; asking
    # where half of what lies below is rough fires well above a real treeline.
    # The transition that maximises (roughness below - roughness above) is the
    # horizon, and requiring that contrast to be decisive leaves an all-sky
    # frame alone.
    rows, cols = rough.shape
    skyline = np.full(cols, rows, dtype=float)
    for col in range(cols):
        column = rough[:, col]
        if column.mean() >= 0.6:
            # Rough top to bottom: a tree filling the column, not a horizon.
            # Without this the absence of a step reads as "all sky", which is
            # how porch lights behind the treeline got back in.
            skyline[col] = 0.0
            continue
        best_row, best_score = None, 0.0
        for row in range(1, rows):
            score = column[row:].mean() - column[:row].mean()
            if score > best_score:
                best_row, best_score = row, score
        if best_row is not None and best_score >= 0.4:
            skyline[col] = best_row
    return skyline * block * factor, block * factor


def in_sky(source, skyline, tile_px, margin=0):
    """Was this source above the skyline in its own column?

    Its own column, deliberately — not the lowest skyline nearby. The Moon in
    the frame that motivated this sits just above a roofline with a tall tree
    beside it, and taking the minimum over neighbouring columns threw it out
    for being lower than the tree.
    """
    col = min(int(source["x"] // tile_px), len(skyline) - 1)
    return source["y"] < float(skyline[col]) - margin


# --- the check that actually identifies the Moon ----------------------------

# A crescent's bright limb faces the Sun. Nothing else in a night photo has to
# obey that, which makes it the discriminator the size and field gates aren't:
# on the frame this was built against, those gates left four candidates and the
# true Moon was not even the best-scoring one. By limb direction the Moon
# missed by 6 degrees and the three impostors by 58, 60 and 71.
MAX_LIMB_DISAGREEMENT_DEG = 30.0


def limb_direction(image, x, y, extent):
    """Angle from a blob's overall centroid to its brightest part, in degrees
    measured in pixel space. For a crescent that is the illuminated limb."""
    r = max(6, int(extent))
    y0, y1 = max(0, int(y) - r), min(image.shape[0], int(y) + r + 1)
    x0, x1 = max(0, int(x) - r), min(image.shape[1], int(x) + r + 1)
    win = image[y0:y1, x0:x1].astype(np.float32)
    win = win - np.percentile(win, 40)
    peak = float(win.max())
    if peak <= 0:
        return None

    def centroid(mask):
        yy, xx = np.nonzero(mask)
        if len(xx) == 0:
            return None
        vals = win[yy, xx]
        return ((xx * vals).sum() / vals.sum(), (yy * vals).sum() / vals.sum())

    whole, bright = centroid(win > peak * 0.30), centroid(win > peak * 0.75)
    if whole is None or bright is None:
        return None
    dx, dy = bright[0] - whole[0], bright[1] - whole[1]
    # The shift has to be a real fraction of the object, not a pixel of noise.
    # A small round blob's bright centroid wanders at random, and a wandering
    # direction lines up with the Sun often enough to matter: a 20 px lamp
    # scored a 0.3 degree "agreement" and outranked the actual Moon.
    # Measured on a real dusk frame: the crescent Moon shifts 2.9 px, a tree
    # edge 2.5 px, a distant lamp 0.3 px. The floor throws out the lamp, whose
    # direction is pure noise and lined up with the Sun to within 0.3 degrees
    # by luck; the tree is left for the direction check, which misses by 60.
    if math.hypot(dx, dy) < max(1.5, 0.03 * extent):
        return None
    return math.degrees(math.atan2(dy, dx))


def solar_limb_offset(image, wcs, source, sun_radec):
    """Degrees between where a blob's bright limb points and where the Sun is,
    or None if the blob has no measurable limb."""
    measured = limb_direction(image, source["x"], source["y"], source["extent"])
    if measured is None:
        return None
    sx, sy = wcs.all_world2pix(sun_radec[0], sun_radec[1], 0)
    to_sun = math.degrees(math.atan2(float(sy) - source["y"],
                                     float(sx) - source["x"]))
    return abs(((measured - to_sun + 180.0) % 360.0) - 180.0)


def register_frame(image_path, exif_info, bodies, sun_radec, sources=None):
    """Best registration for this frame, or None.

    `bodies` are the solar-system objects above the horizon with ra/dec/kind,
    `sun_radec` positions the Sun for the crescent check. Returns the winning
    match with its WCS and the evidence behind it, so a caller can decide how
    loudly to claim it.
    """
    from PIL import Image

    width, height = exif_info.get("width"), exif_info.get("height")
    if not width or not height or len(bodies) < 2:
        return None

    if sources is None:
        skyline, tile = sky_mask(image_path)
        sources = [s for s in detect_sources(image_path)
                   if in_sky(s, skyline, tile)]
    if len(sources) < 2:
        return None

    with Image.open(image_path) as im:
        image = np.asarray(im.convert("L"), dtype=np.float32)

    scored = []
    for match in match_sources_to_bodies(sources, bodies, width, height,
                                         exif_info.get("fov_bounds")):
        moon_index = next((i for i, name in enumerate(match["bodies"])
                           if name == "Moon"), None)
        if moon_index is None:
            # No Moon in the pair: nothing here can check the identification,
            # so refuse rather than guess. Planet-only registration needs a
            # third body to verify against.
            continue
        offset = solar_limb_offset(image, match["wcs"],
                                   match["sources"][moon_index], sun_radec)
        if offset is None or offset > MAX_LIMB_DISAGREEMENT_DEG:
            continue
        scored.append(dict(match, limb_offset_deg=round(offset, 1)))

    if not scored:
        return None
    scored.sort(key=lambda m: (m["limb_offset_deg"], m["moon_score"]))
    best = scored[0]
    best["alternatives"] = len(scored) - 1
    return best


def register_from_anchors(image_path, exif_info, anchors, bodies, snap_px=120):
    """Register from positions a person pointed at (#85).

    `anchors` are [{"name": "Moon", "x": .., "y": ..}, ...] in image pixels.
    Each is snapped to the nearest detected source so a fingertip on a phone
    doesn't have to be accurate, then the pair is fitted exactly.

    This is the reliable half of the feature. Automatic identification picks
    the wrong blob on real frames; a person looking at their own photo does
    not, and the geometry after that is sub-pixel.
    """
    width, height = exif_info.get("width"), exif_info.get("height")
    if not width or not height or len(anchors) < 2:
        return None
    by_name = {b["name"]: b for b in bodies}
    if not all(a.get("name") in by_name for a in anchors[:2]):
        return None

    sources = detect_sources(image_path)
    placed = []
    for anchor in anchors[:2]:
        x, y = float(anchor["x"]), float(anchor["y"])
        near = [s for s in sources
                if math.hypot(s["x"] - x, s["y"] - y) <= snap_px]
        if near:
            best = min(near, key=lambda s: math.hypot(s["x"] - x, s["y"] - y))
            placed.append({"name": anchor["name"], "x": best["x"],
                           "y": best["y"], "extent": best["extent"],
                           "snapped": True})
        else:
            # Nothing detected under the finger: trust the tap. Worse
            # astrometry than a centroid, but the person can see the object
            # and the detector evidently cannot.
            placed.append({"name": anchor["name"], "x": x, "y": y,
                           "extent": 0, "snapped": False})

    a, b = placed
    wcs = wcs_from_pair((a["x"], a["y"]),
                        (by_name[a["name"]]["ra"], by_name[a["name"]]["dec"]),
                        (b["x"], b["y"]),
                        (by_name[b["name"]]["ra"], by_name[b["name"]]["dec"]),
                        width, height)
    if wcs is None:
        return None
    field = angular_separation(*wcs.all_pix2world(0, height / 2, 0),
                               *wcs.all_pix2world(width - 1, height / 2, 0))
    return {"wcs": wcs, "anchors": placed, "field_deg": round(field, 2),
            "bodies": [a["name"], b["name"]]}
