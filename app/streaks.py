"""Streaks in the pixels: the line a meteor or a satellite leaves across a
long exposure, found in the image itself and then explained through the
solve.

The satellite layer (`app.satellites`) works the other way round — it
predicts where known objects were and draws the computed track, never
looking at the pixels. That leaves the most exciting thing a phone can
catch, a meteor, unmentioned: a real one, 5 degrees long below the
Dipper's handle, went through the site as a plain star field. This
module closes that gap.

Detection is a Hough transform over a cleaned mask of the frame:
background-flattened, then a min-of-medians top-hat so only *thin*
structure survives (a horizon step or a light beam is wide; a streak is
two pixels), stars removed by the same isolation test as the pre-solve
gate, and the blocks where the mask is dense — ground, foliage, lit sand
— dropped outright, since texture is made of short straight bits and
would vote for lines all night. Each accumulator peak is then checked
against the pixels: the longest well-covered run along the line, made of
stretches rather than a chain of star-sized clumps, sky at the same level
on both sides of it and past both ends (a sharpened horizon has sky on
one side only; a lamp arm or a branch is attached to something), empty
sky a little way out (twigs come with twigs), no bright star centred on
it (diffraction spikes). Tuned on a real corpus of phone night shots;
every rule above is there because that corpus produced the false
positive it names.

Classification is by what the solve and the EXIF make measurable. A
predicted crossing lying on the streak names the satellite outright. A
streak too long for any satellite to have covered in the exposure at that
elevation is a meteor. Otherwise the brightness profile along the line
decides: a satellite is the same brightness end to end and is cut off by
the shutter at both ends, while a meteor fades in, brightens as it
ablates, and stops. Meteors are then back-projected against the active
showers' radiants; a miss means sporadic, which most are.

Everything here is best-effort and honest about doubt: a streak the
rules can't place is reported as a streak, with the measurements, not
guessed at.
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image

from . import ephemeris, night

# The frame is scaled to this width before detection, as the pre-solve
# star gate does: the pixel-unit tunables below are meant for one scale.
WORK_WIDTH = 1600
BG_GRID = 24                # block-median background, blocks per axis
TOPHAT_K = 13               # median window; structure wider than ~K/2 vanishes
MASK_SIGMA = 3.0            # line-support threshold over the top-hat noise...
MASK_MIN_AMP = 6.0          # ...but never below this many ADU
STAR_SIGMA = 5.0            # star candidates must clear this
STAR_MIN_AMP = 10.0
STAR_RADIUS = 6             # mask disc around each isolated star
TEXTURE_BLOCK = 64          # foreground test: mask density per block...
TEXTURE_MAX_DENSITY = 0.10  # ...sky sits under 0.05, lit ground over 0.15
TEXTURE_GROW = 2            # blocks: twigs reach past the tree's own blocks
THETA_STEP_DEG = 0.5
BAND_PX = 2.0               # a pixel supports a line if within this of it
RETIRE_PX = 5.0             # support this close to a found run is spent: the
                            # wings of a bright trail would otherwise re-run
GAP_PX = 16                 # bridge breaks up to this along the run: stacking
                            # seams, and the disc masked over a star it crosses
MIN_LENGTH_PX = 40          # a degree or two on a phone frame; a handheld
                            # shot's trailed stars stay under this
MIN_COVERAGE = 0.75         # fraction of the run with a supporting pixel...
DASHED_MIN_LENGTH_PX = 80   # ...unless it is long enough that a broken
DASHED_MIN_COVERAGE = 0.5   # line (a stacked satellite) is still no accident
# A chance line through a few fat stars (a cluster, a rich field) is a
# chain of short clumps, not a line: many pieces, each a star wide.
CHAIN_GAP_PX = 2.0
CHAIN_MIN_PIECES = 3
CHAIN_MAX_PIECE_PX = 12.0   # median piece shorter than this: a chain
# A second peak a few pixels over at the same angle is the same streak
# (the two edges of a bright, wide one), not another.
DUPLICATE_ANGLE_DEG = 5.0
DUPLICATE_DIST_PX = 12.0
MAX_CANDIDATES = 12         # accumulator peaks examined
MAX_STREAKS = 4
PROFILE_HALF_WIDTH = 2      # perpendicular pixels searched for the streak's peak
MIN_PEAK_AMP = 12.0         # ADU over sky at the brightest; the soft ridge
                            # along a cloud's edge sits under 10
PROFILE_SMOOTH = 7
PROFILE_SAMPLES = 40        # what is stored, resampled
# Both sides of a streak must be sky. Sky is the raw level of the blocks
# that hold stars; the bands either side of the line must sit near it and
# agree with each other (a sharpened roofline has sky on one side only),
# and the wider bands beyond must not fall into a dark mass either (a lit
# rim a few pixels inside a silhouette).
SIDE_OFFSET = (4, 9)        # px either side of the line
FAR_OFFSET = (12, 30)
SIDE_MAX_DIFF_SIGMA = 4.0   # sides agree to within this many noise sigma...
SIDE_MAX_DIFF_FRAC = 0.25   # ...or this fraction of the sky level, whichever is more
SIDE_MIN_SKY_FRAC = 0.5     # and neither side, near or far, below this much sky
# Beyond each end the line must run out into sky too: a lamp arm or a
# branch is attached to something at one end. Ends at the frame edge are
# exempt, since the streak may simply leave the frame.
CAP_OFFSET = (4, 16)        # px past the end, along the line
CAP_HALF_WIDTH = 3
CAP_TOL_FACTOR = 1.25       # a cloud past one end is not an attachment
EDGE_MARGIN_PX = 16
# And the sky beside it must be empty: the support mask a little way out
# from the line, per unit length. Twigs and leaves clutter it; a streak's
# surroundings hold only noise and the odd star.
CLUTTER_OFFSET = (10, 25)
CLUTTER_MAX_PER_PX = 0.4
SPIKE_STAR_AMP = 60.0       # a star this bright at the middle of the run: a
SPIKE_MID_FRAC = 0.3        # diffraction spike, which is symmetric about its star

# Profile shape. Flatness is the interior's 20th/80th percentile ratio;
# taper is how far along the run, as a fraction, the light takes to reach
# half its peak from an end. Measured on a real iPhone meteor: flatness
# 0.59, taper 0.25 at the onset end and 0.04 at the terminal one. A
# synthetic uniform trail sits at flatness > 0.85, taper < 0.05.
METEOR_MAX_FLATNESS = 0.7
METEOR_MIN_TAPER = 0.12
SATELLITE_MIN_FLATNESS = 0.7
SATELLITE_MAX_TAPER = 0.08
PROFILE_END_TRIM = 0.1      # interior = the run minus this fraction at each end
# Breaks at regular intervals along the run are the sub-frame boundaries
# of a stacked night-mode exposure: the object moved steadily through
# several frames. Whether that is a satellite crossing the whole exposure
# or a meteor spanning a few short frames depends on the frame length,
# which the EXIF does not say — so the verdict stays low unless the
# speed settles it. Measured on the real iPhone meteor: dips to 40% at
# 97 and 201 px of 299, pieces 97/104/98.
BREAK_DEPTH = 0.65          # a dip below this fraction of its neighbourhood's median...
BREAK_WINDOW_PX = 12        # ...within this many px either side, work scale
BREAK_MIN_PIECES = 3
BREAK_REGULARITY = 0.35     # (longest - shortest) / mean piece, at most

# Fastest a satellite can cross the sky: a 400 km orbit (the ISS, and
# little visible sits lower) at 7.7 km/s, seen at the streak's elevation,
# with a margin for Earth's rotation and the odd decaying object.
SAT_ORBIT_KM = 400.0
SAT_SPEED_KM_S = 7.7
EARTH_RADIUS_KM = 6371.0
SAT_RATE_MARGIN = 1.15
SAT_MIN_RATE_DEG_S = 0.03   # slower than this over the exposure: not a satellite
SAT_MATCH_FRAC = 0.015      # predicted track within this fraction of width...
SAT_MATCH_ANGLE_DEG = 6.0   # ...and this many degrees of the streak's angle

# Shower radiants: IMO working list, the showers a phone has any chance
# of catching. (peak RA, peak Dec) in degrees at the peak date, activity
# window as (month, day) pairs, and the radiant's daily drift in RA and
# Dec, so a Perseid on August 1st is looked for where the radiant was on
# August 1st. Values are rounded; the match tolerance absorbs that.
SHOWERS = [
    ("Quadrantids", (12, 28), (1, 12), (1, 3), 230.0, 49.0, 0.8, -0.2),
    ("Lyrids", (4, 14), (4, 30), (4, 22), 271.0, 34.0, 1.1, 0.0),
    ("eta Aquariids", (4, 19), (5, 28), (5, 6), 338.0, -1.0, 0.9, 0.4),
    ("Southern delta Aquariids", (7, 12), (8, 23), (7, 30), 340.0, -16.0, 0.8, 0.2),
    ("alpha Capricornids", (7, 3), (8, 15), (7, 31), 307.0, -10.0, 0.9, 0.3),
    ("Perseids", (7, 17), (8, 24), (8, 12), 48.0, 58.0, 1.4, 0.3),
    ("kappa Cygnids", (8, 3), (8, 25), (8, 17), 286.0, 59.0, 0.3, 0.2),
    ("Aurigids", (8, 28), (9, 5), (9, 1), 91.0, 39.0, 1.1, 0.0),
    ("September epsilon Perseids", (9, 5), (9, 21), (9, 9), 48.0, 40.0, 1.1, 0.3),
    ("Draconids", (10, 6), (10, 10), (10, 8), 262.0, 54.0, 0.0, 0.0),
    ("Southern Taurids", (9, 10), (11, 20), (10, 10), 32.0, 9.0, 0.8, 0.2),
    ("Orionids", (10, 2), (11, 7), (10, 21), 95.0, 16.0, 0.7, 0.1),
    ("Northern Taurids", (10, 20), (12, 10), (11, 12), 58.0, 22.0, 0.8, 0.2),
    ("Leonids", (11, 6), (11, 30), (11, 17), 152.0, 22.0, 0.7, -0.4),
    ("Geminids", (12, 4), (12, 17), (12, 14), 112.0, 33.0, 1.0, -0.1),
    ("Ursids", (12, 17), (12, 26), (12, 22), 217.0, 76.0, 0.0, 0.0),
]
# How far the radiant may sit from the streak's great circle. Radiants
# are a few degrees across and drift; a short streak's direction is only
# good to a few degrees itself.
SHOWER_MAX_OFFSET_DEG = 10.0
SHOWER_MIN_RADIANT_DIST_DEG = 3.0   # a streak can't start inside its own radiant
SHOWER_MAX_RADIANT_DIST_DEG = 120.0


# --- pixels -----------------------------------------------------------------

def _load_work(image_path):
    """Grayscale float frame at WORK_WIDTH, and the scale applied."""
    with Image.open(image_path) as src:
        img = src.convert("L")
        scale = 1.0
        if img.width > WORK_WIDTH:
            scale = WORK_WIDTH / img.width
            img = img.resize(
                (WORK_WIDTH, max(1, round(img.height * scale))), Image.LANCZOS)
        return np.asarray(img, dtype=np.float32), scale


def _block_background(a, grid=BG_GRID):
    h, w = a.shape
    bh, bw = max(8, h // grid), max(8, w // grid)
    H, W = (h // bh) * bh, (w // bw) * bw
    med = np.median(a[:H, :W].reshape(H // bh, bh, W // bw, bw), axis=(1, 3))
    bg = np.empty_like(a)
    bg[:H, :W] = np.kron(med, np.ones((bh, bw), dtype=np.float32))
    bg[H:, :W] = bg[H - 1:H, :W]
    bg[:, W:] = bg[:, W - 1:W]
    return bg


def _median_1d(a, k, axis):
    pad = [(0, 0), (0, 0)]
    pad[axis] = (k // 2, k // 2)
    win = np.lib.stride_tricks.sliding_window_view(
        np.pad(a, pad, mode="edge"), k, axis=axis)
    return np.median(win, axis=-1).astype(np.float32)


def _tophat(det):
    """Thin-structure image: the frame minus the lower of its row-wise and
    column-wise running medians. A line survives whichever window crosses
    it; anything wider than half the window is its own local background
    and cancels. (A full 2D median would do the same at 80x the memory.)"""
    local = np.minimum(_median_1d(det, TOPHAT_K, 0), _median_1d(det, TOPHAT_K, 1))
    return det - local


def _star_mask(top, sigma, shape):
    """Discs over every isolated peak — the pre-solve gate's star test —
    so a star's core can't lend votes to lines through it. Returns
    (mask, [(x, y, amp)]) with the peaks kept for the spike check."""
    h, w = shape
    neigh = np.full_like(top, -np.inf)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == dx == 0:
                continue
            np.maximum(neigh, np.roll(np.roll(top, dy, 0), dx, 1), out=neigh)
    thr = max(STAR_SIGMA * sigma, STAR_MIN_AMP)
    ys, xs = np.where((top > thr) & (top >= neigh))
    r = STAR_RADIUS
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    disc = (yy ** 2 + xx ** 2) <= r * r
    mask = np.zeros(shape, dtype=bool)
    stars = []
    for y, x in zip(ys, xs):
        if y < 12 or x < 12 or y >= h - 12 or x >= w - 12:
            continue
        amp = top[y, x]
        ring = top[y - 10:y + 11, x - 10:x + 11].copy()
        ring[7:14, 7:14] = -np.inf
        if ring.max() < max(0.35 * amp, thr):
            mask[y - r:y + r + 1, x - r:x + r + 1] |= disc
            stars.append((float(x), float(y), float(amp)))
    return mask, stars


def _texture_mask(mask):
    """Blocks where the support mask is dense, grown by one block: the
    ground, and whatever else is made of short bright edges."""
    h, w = mask.shape
    B = TEXTURE_BLOCK
    nh, nw = (h + B - 1) // B, (w + B - 1) // B
    density = np.zeros((nh, nw), dtype=np.float32)
    H, W = (h // B) * B, (w // B) * B
    if H and W:
        density[:H // B, :W // B] = \
            mask[:H, :W].reshape(H // B, B, W // B, B).mean(axis=(1, 3))
    dense = density > TEXTURE_MAX_DENSITY
    grown = dense.copy()
    r = TEXTURE_GROW
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            grown |= np.roll(np.roll(dense, dy, 0), dx, 1)
    return np.kron(grown, np.ones((B, B), dtype=bool))[:h, :w]


def _support_mask(image_path):
    """(raw frame, top-hat image, support mask, noise sigma, scale, stars)."""
    img, scale = _load_work(image_path)
    det = img - _block_background(img)
    top = _tophat(det)
    sigma = 1.4826 * float(np.median(np.abs(top - np.median(top))))
    mask = top > max(MASK_SIGMA * sigma, MASK_MIN_AMP)
    star_mask, stars = _star_mask(top, sigma, mask.shape)
    mask &= ~star_mask
    mask[:4, :] = mask[-4:, :] = False
    mask[:, :4] = mask[:, -4:] = False
    mask &= ~_texture_mask(mask)
    return img, top, mask, sigma, scale, stars


def _hough(mask, thetas, diag):
    ys, xs = np.where(mask)
    acc = np.zeros((len(thetas), 2 * diag + 1), dtype=np.int32)
    if len(xs) == 0:
        return acc
    rho = np.rint(xs[:, None] * np.cos(thetas)[None, :]
                  + ys[:, None] * np.sin(thetas)[None, :]).astype(np.int64) + diag
    for i in range(len(thetas)):
        acc[i] = np.bincount(rho[:, i], minlength=2 * diag + 1)
    return acc


def _longest_run(theta, rho, mask):
    """The longest stretch of the line (theta, rho) with supporting pixels
    no more than GAP_PX apart: (t0, t1, coverage, n, chain) in along-line
    coordinates, or None. `chain` says the support is a string of short
    clumps rather than a line."""
    ys, xs = np.where(mask)
    c, s = math.cos(theta), math.sin(theta)
    near = np.abs(xs * c + ys * s - rho) <= BAND_PX
    if not near.any():
        return None
    t = np.sort(-xs[near] * s + ys[near] * c)
    # Pieces: stretches of support with no gap over CHAIN_GAP_PX...
    breaks = np.where(np.diff(t) > CHAIN_GAP_PX)[0]
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, len(t) - 1]
    pieces = t[ends] - t[starts]
    # ...joined across gaps up to GAP_PX into the candidate runs.
    joins = np.where(t[starts[1:]] - t[ends[:-1]] > GAP_PX)[0]
    run_starts = np.r_[0, joins + 1]
    run_ends = np.r_[joins, len(starts) - 1]
    k = int(np.argmax(t[ends[run_ends]] - t[starts[run_starts]]))
    first, last = run_starts[k], run_ends[k]
    run = t[starts[first]:ends[last] + 1]
    t0, t1 = float(run[0]), float(run[-1])
    covered = len(np.unique(np.rint(run)))
    run_pieces = pieces[first:last + 1]
    chain = len(run_pieces) >= CHAIN_MIN_PIECES and \
        float(np.median(run_pieces)) < CHAIN_MAX_PIECE_PX
    return t0, t1, covered / max(1.0, t1 - t0 + 1), len(run), chain


def _same_line(theta, rho, t0, t1, ptheta, prho, pt0, pt1):
    """Whether a run is a repeat of an accepted one: near-parallel, a few
    pixels over, overlapping along its length."""
    dtheta = abs(math.degrees(theta - ptheta)) % 180.0
    if min(dtheta, 180.0 - dtheta) > DUPLICATE_ANGLE_DEG:
        return False
    mx, my = _line_point(theta, rho, (t0 + t1) / 2.0)
    c, s = math.cos(ptheta), math.sin(ptheta)
    if abs(mx * c + my * s - prho) > DUPLICATE_DIST_PX:
        return False
    along = -mx * s + my * c
    return pt0 - GAP_PX <= along <= pt1 + GAP_PX


def _line_point(theta, rho, t, offset=0.0):
    c, s = math.cos(theta), math.sin(theta)
    return (rho + offset) * c - t * s, (rho + offset) * s + t * c


def _sample(img, theta, rho, ts, offset):
    """img at the line's points for each t, at a perpendicular offset;
    NaN off-frame."""
    h, w = img.shape
    c, s = math.cos(theta), math.sin(theta)
    xs = np.rint((rho + offset) * c - ts * s).astype(int)
    ys = np.rint((rho + offset) * s + ts * c).astype(int)
    ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    out = np.full(len(ts), np.nan, dtype=np.float32)
    out[ok] = img[ys[ok], xs[ok]]
    return out


def _profile(top, theta, rho, t0, t1, smooth=PROFILE_SMOOTH):
    """Brightness along the run: the top-hat's peak across the streak's
    width at each step, smoothed. NaN-free."""
    ts = np.arange(math.floor(t0), math.ceil(t1) + 1, dtype=np.float32)
    stack = np.stack([_sample(top, theta, rho, ts, d)
                      for d in range(-PROFILE_HALF_WIDTH, PROFILE_HALF_WIDTH + 1)])
    prof = np.nanmax(np.where(np.isnan(stack), -np.inf, stack), axis=0)
    prof = np.where(np.isfinite(prof), prof, 0.0)
    k = min(smooth, len(prof))
    if k > 1:
        prof = np.convolve(np.pad(prof, (k // 2, k - 1 - k // 2), mode="edge"),
                           np.ones(k) / k, mode="valid")
    return prof


def _side_levels(img, theta, rho, t0, t1, offsets):
    """Median raw level in a band `offsets` px out, either side of the run."""
    ts = np.arange(math.floor(t0), math.ceil(t1) + 1, dtype=np.float32)
    levels = []
    for sign in (-1, 1):
        vals = np.concatenate([_sample(img, theta, rho, ts, sign * d)
                               for d in range(offsets[0], offsets[1] + 1)])
        vals = vals[np.isfinite(vals)]
        levels.append(float(np.median(vals)) if len(vals) else 0.0)
    return levels


def _sky_level(img, stars):
    """The raw level of sky: the median over the blocks that hold stars.
    Foreground can hold bright specks too (a lit tree), but not most of
    the frame's isolated peaks."""
    h, w = img.shape
    B = TEXTURE_BLOCK
    if not stars:
        return float(np.median(img))
    cells = {(int(y) // B, int(x) // B) for x, y, _ in stars}
    levels = [np.median(img[by * B:(by + 1) * B, bx * B:(bx + 1) * B])
              for by, bx in cells]
    return float(np.median(levels))


def _cap_levels(img, theta, rho, t0, t1):
    """Median raw level just past each end of the run, along the line;
    None for an end at the frame edge."""
    h, w = img.shape
    levels = []
    for t_end, sign in ((t0, -1), (t1, 1)):
        x, y = _line_point(theta, rho, t_end)
        if min(x, y, w - 1 - x, h - 1 - y) < EDGE_MARGIN_PX:
            levels.append(None)
            continue
        ts = t_end + sign * np.arange(CAP_OFFSET[0], CAP_OFFSET[1] + 1, dtype=np.float32)
        vals = np.concatenate([_sample(img, theta, rho, ts, d)
                               for d in range(-CAP_HALF_WIDTH, CAP_HALF_WIDTH + 1)])
        vals = vals[np.isfinite(vals)]
        levels.append(float(np.median(vals)) if len(vals) else None)
    return levels


def _in_sky(img, theta, rho, t0, t1, sigma, sky):
    """Whether the run sits in sky: both sides read as sky, near and
    further out, and so does the line's continuation past each end."""
    near = _side_levels(img, theta, rho, t0, t1, SIDE_OFFSET)
    far = _side_levels(img, theta, rho, t0, t1, FAR_OFFSET)
    tol = max(SIDE_MAX_DIFF_SIGMA * max(sigma, 1.0), SIDE_MAX_DIFF_FRAC * sky)
    if abs(near[0] - near[1]) > tol:
        return False
    if min(near + far) < SIDE_MIN_SKY_FRAC * sky:
        return False
    local = (near[0] + near[1]) / 2.0
    return all(cap is None or abs(cap - local) <= CAP_TOL_FACTOR * tol
               for cap in _cap_levels(img, theta, rho, t0, t1))


def _clutter(mask, theta, rho, t0, t1):
    """Support pixels a little way out from the run, per unit length."""
    ys, xs = np.where(mask)
    c, s = math.cos(theta), math.sin(theta)
    d = np.abs(xs * c + ys * s - rho)
    t = -xs * s + ys * c
    beside = (d > CLUTTER_OFFSET[0]) & (d <= CLUTTER_OFFSET[1]) & (t >= t0) & (t <= t1)
    return float(beside.sum()) / max(1.0, t1 - t0)


def _shape(prof):
    """(flatness, taper_start, taper_end) of a profile, see the tunables."""
    n = len(prof)
    peak = float(prof.max()) if n else 0.0
    if n < 8 or peak <= 0:
        return 1.0, 0.0, 0.0
    trim = max(1, int(n * PROFILE_END_TRIM))
    inner = prof[trim:n - trim]
    p20, p80 = np.percentile(inner, 20), np.percentile(inner, 80)
    flat = float(p20 / p80) if p80 > 0 else 1.0
    half = 0.5 * peak
    rise_a = int(np.argmax(prof >= half)) / n
    rise_b = int(np.argmax(prof[::-1] >= half)) / n
    return flat, rise_a, rise_b


def _periodic_breaks(prof):
    """Whether the profile dips at regular intervals: the positions of
    the dips, or [] when there are too few or they are uneven."""
    n, w = len(prof), BREAK_WINDOW_PX
    dips = []
    for i in range(w, n - w):
        window = prof[i - w:i + w + 1]
        # Against the window's median, not its max: the valley beside a
        # star the streak crosses is not a break.
        if prof[i] <= window.min() and prof[i] < BREAK_DEPTH * np.median(window):
            if not dips or i - dips[-1] > w:
                dips.append(i)
    if len(dips) + 1 < BREAK_MIN_PIECES:
        return []
    edges = [0] + dips + [n - 1]
    pieces = np.diff(edges)
    if (pieces.max() - pieces.min()) / pieces.mean() > BREAK_REGULARITY:
        return []
    return dips


def _resample(prof, n=PROFILE_SAMPLES):
    if len(prof) == 0:
        return []
    xs = np.linspace(0, len(prof) - 1, n)
    return [round(float(v), 1) for v in np.interp(xs, np.arange(len(prof)), prof)]


def detect(image_path):
    """Streaks in the frame, in full-resolution pixel coordinates.

    Returns {"streaks": [...], "noise_sigma": float, "work_scale": float}.
    Each streak: start/end (x, y), length_px, width_px, coverage,
    peak_amp, profile (PROFILE_SAMPLES values start to end), and the
    shape numbers the classifier uses. `start` is the end the light came
    from when the profile says so (the longer taper), else arbitrary.
    """
    img, top, mask, sigma, scale, stars = _support_mask(image_path)
    sky = _sky_level(img, stars)
    support = mask.copy()  # the full mask; `mask` is spent as lines are found
    h, w = mask.shape
    thetas = np.deg2rad(np.arange(0.0, 180.0, THETA_STEP_DEG))
    diag = int(math.hypot(h, w)) + 1
    star_xy = np.array([(x, y, a) for x, y, a in stars]).reshape(-1, 3)
    streaks = []
    found_lines = []  # (theta, rho, t0, t1) of accepted runs, work scale
    examined = 0
    while len(streaks) < MAX_STREAKS and examined < MAX_CANDIDATES:
        acc = _hough(mask, thetas, diag)
        if acc.max() < MIN_LENGTH_PX * MIN_COVERAGE:
            break
        ti, ri = divmod(int(acc.argmax()), acc.shape[1])
        theta, rho = float(thetas[ti]), float(ri - diag)
        examined += 1
        run = _longest_run(theta, rho, mask)
        # Whatever the verdict, retire this line's support so the next
        # peak is a different line and not this one's neighbouring bin.
        ys, xs = np.where(mask)
        c, s = math.cos(theta), math.sin(theta)
        along = -xs * s + ys * c
        if run is None:
            near = np.abs(xs * c + ys * s - rho) <= BAND_PX
            mask[ys[near], xs[near]] = False
            continue
        t0, t1, coverage, n, chain = run
        kill = (np.abs(xs * c + ys * s - rho) <= RETIRE_PX) \
            & (along >= t0 - GAP_PX) & (along <= t1 + GAP_PX)
        mask[ys[kill], xs[kill]] = False
        length = t1 - t0
        long_enough = (length >= MIN_LENGTH_PX and coverage >= MIN_COVERAGE) or \
            (length >= DASHED_MIN_LENGTH_PX and coverage >= DASHED_MIN_COVERAGE)
        if not long_enough or chain:
            continue
        if any(_same_line(theta, rho, t0, t1, *prev) for prev in found_lines):
            continue
        # A sharpened roofline, a lit rim inside a silhouette, a lamp arm:
        # not sky all round.
        if not _in_sky(img, theta, rho, t0, t1, sigma, sky):
            continue
        # A twig among twigs: the sky beside a streak is empty.
        if _clutter(support, theta, rho, t0, t1) > CLUTTER_MAX_PER_PX:
            continue
        # A diffraction spike: a bright star at the middle of the run. (A
        # streak may cross a star anywhere else; a spike is centred.)
        if len(star_xy):
            d = np.abs(star_xy[:, 0] * c + star_xy[:, 1] * s - rho)
            st = -star_xy[:, 0] * s + star_xy[:, 1] * c
            mid, half = (t0 + t1) / 2.0, length * SPIKE_MID_FRAC / 2.0
            on = (d <= BAND_PX + 2) & (np.abs(st - mid) <= half) \
                & (star_xy[:, 2] >= SPIKE_STAR_AMP)
            if on.any():
                continue
        prof = _profile(top, theta, rho, t0, t1)
        if prof.max() < MIN_PEAK_AMP:
            continue
        flat, rise_a, rise_b = _shape(prof)
        # Breaks are a few pixels wide: read them off the unsmoothed run.
        breaks = _periodic_breaks(_profile(top, theta, rho, t0, t1, smooth=1))
        a, b = _line_point(theta, rho, t0), _line_point(theta, rho, t1)
        # Start where the light fades in, if one end clearly does.
        if rise_b > rise_a:
            a, b = b, a
            prof = prof[::-1]
            rise_a, rise_b = rise_b, rise_a
        found_lines.append((theta, rho, t0, t1))
        streaks.append({
            "start": [round(a[0] / scale, 1), round(a[1] / scale, 1)],
            "end": [round(b[0] / scale, 1), round(b[1] / scale, 1)],
            "length_px": round(length / scale, 1),
            "width_px": round(n / max(1.0, length) / scale, 2),
            "coverage": round(coverage, 2),
            "dashed": coverage < MIN_COVERAGE,
            "periodic_breaks": len(breaks),
            "peak_amp": round(float(prof.max()), 1),
            "profile": _resample(prof),
            "flatness": round(flat, 2),
            "taper_start": round(rise_a, 2),
            "taper_end": round(rise_b, 2),
            "angle_deg": round(math.degrees(theta), 1),
        })
    return {"streaks": streaks, "noise_sigma": round(sigma, 2),
            "sky_level": round(sky, 1), "work_scale": round(scale, 4)}


# --- sky ----------------------------------------------------------------------

def _unit(ra, dec):
    r, d = math.radians(ra), math.radians(dec)
    return np.array([math.cos(d) * math.cos(r), math.cos(d) * math.sin(r),
                     math.sin(d)])


def _sep_deg(ra1, dec1, ra2, dec2):
    v = float(np.clip(np.dot(_unit(ra1, dec1), _unit(ra2, dec2)), -1.0, 1.0))
    return math.degrees(math.acos(v))


def _sat_max_rate(alt_deg):
    """Fastest apparent motion, deg/s, of a satellite at SAT_ORBIT_KM seen
    at this elevation: orbital speed over the slant range."""
    e = math.radians(max(0.0, alt_deg))
    R, H = EARTH_RADIUS_KM, SAT_ORBIT_KM
    slant = math.sqrt((R + H) ** 2 - (R * math.cos(e)) ** 2) - R * math.sin(e)
    return math.degrees(SAT_SPEED_KM_S / slant) * SAT_RATE_MARGIN


def _elevation(ra, dec, exif_info, when_utc):
    """Highest the point could have stood for any observer the EXIF
    allows (GPS, or the timezone band) — the conservative choice for a
    speed limit. None when the EXIF says nothing about where."""
    if when_utc is None:
        return None
    positions, _ = night.observers(exif_info, (ra, dec), when_utc)
    if not positions:
        return None
    gmst = float(ephemeris._timescale().from_datetime(when_utc).gmst)
    return max(night._altitude_deg(ra, dec, la, lo, gmst) for la, lo in positions)


def _radiant_on(date, entry):
    """The radiant on `date` if the shower is active then, else None."""
    name, (m0, d0), (m1, d1), (mp, dp), ra, dec, dra, ddec = entry
    year = date.year
    start = datetime(year, m0, d0, tzinfo=timezone.utc)
    end = datetime(year, m1, d1, tzinfo=timezone.utc)
    peak = datetime(year, mp, dp, tzinfo=timezone.utc)
    if end < start:  # window straddles New Year (Quadrantids)
        if date.month == m0 or (date.month == 12 and date.day >= d0):
            start = datetime(year, m0, d0, tzinfo=timezone.utc)
            end = datetime(year + 1, m1, d1, tzinfo=timezone.utc)
            peak = datetime(year + (1 if mp < m0 else 0), mp, dp, tzinfo=timezone.utc)
        else:
            start = datetime(year - 1, m0, d0, tzinfo=timezone.utc)
            end = datetime(year, m1, d1, tzinfo=timezone.utc)
            peak = datetime(year if mp < m0 else year - 1, mp, dp, tzinfo=timezone.utc)
    if not (start <= date <= end + timedelta(days=1)):
        return None
    days = (date - peak).total_seconds() / 86400.0
    return name, (ra + dra * days) % 360.0, dec + ddec * days


def match_shower(start_radec, end_radec, when_utc):
    """The active shower whose radiant the streak points back to, as
    {"name", "offset_deg", "radiant_dist_deg"}, or None. The streak's
    great circle must pass within SHOWER_MAX_OFFSET_DEG of the radiant,
    and the radiant must lie *behind* the start, not ahead of the end."""
    if when_utc is None:
        return None
    a, b = _unit(*start_radec), _unit(*end_radec)
    normal = np.cross(a, b)
    if np.linalg.norm(normal) < 1e-9:
        return None
    normal /= np.linalg.norm(normal)
    forward = np.cross(normal, a)  # tangent at a, pointing toward b
    best = None
    for entry in SHOWERS:
        radiant = _radiant_on(when_utc, entry)
        if radiant is None:
            continue
        name, ra, dec = radiant
        r = _unit(ra, dec)
        offset = math.degrees(math.asin(float(np.clip(np.dot(r, normal), -1, 1))))
        if abs(offset) > SHOWER_MAX_OFFSET_DEG:
            continue
        # Position along the circle relative to a: negative means behind.
        along = math.degrees(math.atan2(float(np.dot(r, forward)), float(np.dot(r, a))))
        if along >= 0:
            continue
        dist = _sep_deg(ra, dec, *start_radec)
        if not (SHOWER_MIN_RADIANT_DIST_DEG <= dist <= SHOWER_MAX_RADIANT_DIST_DEG):
            continue
        cand = {"name": name, "offset_deg": round(abs(offset), 1),
                "radiant_dist_deg": round(dist, 1)}
        if best is None or cand["offset_deg"] < best["offset_deg"]:
            best = cand
    return best


def _crossing_on(streak, crossings, width):
    """The predicted satellite crossing that runs along this streak, if any."""
    (x0, y0), (x1, y1) = streak["start"], streak["end"]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        return None
    nx, ny = -dy / length, dx / length
    tol = SAT_MATCH_FRAC * width
    for c in crossings or []:
        pts = c.get("points") or []
        if len(pts) < 2:
            continue
        # The track's own direction, from its ends.
        tdx, tdy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
        tlen = math.hypot(tdx, tdy)
        if tlen == 0:
            continue
        cosang = abs((dx * tdx + dy * tdy) / (length * tlen))
        if math.degrees(math.acos(min(1.0, cosang))) > SAT_MATCH_ANGLE_DEG:
            continue
        # Every track point near the streak's line, and the streak's
        # midpoint inside the track's extent along it.
        if all(abs((px - x0) * nx + (py - y0) * ny) <= tol for px, py in pts):
            along = [((px - x0) * dx + (py - y0) * dy) / length for px, py in pts]
            if min(along) <= length / 2 <= max(along):
                return c
    return None


def classify(streak, wcs, width, exif_info, crossings, when_utc):
    """Fill in what the solve says about a detected streak, in place."""
    reasons = []
    kind, confidence = "unknown", "low"
    sky = None
    if wcs is not None:
        try:
            ra0, dec0 = (float(v) for v in wcs.all_pix2world(*streak["start"], 0))
            ra1, dec1 = (float(v) for v in wcs.all_pix2world(*streak["end"], 0))
            sky = ((ra0, dec0), (ra1, dec1))
        except Exception:
            sky = None
    if sky:
        streak["start_radec"] = [round(sky[0][0], 3), round(sky[0][1], 3)]
        streak["end_radec"] = [round(sky[1][0], 3), round(sky[1][1], 3)]
        streak["length_deg"] = round(_sep_deg(*sky[0], *sky[1]), 2)

    exposure = exif_info.get("exposure_seconds")
    rate = None
    if sky and exposure:
        rate = streak["length_deg"] / exposure
        streak["rate_deg_s"] = round(rate, 3)

    # 1. A predicted crossing on the line settles it.
    crossing = _crossing_on(streak, crossings, width)
    if crossing:
        streak["satellite"] = {"name": crossing["name"],
                               "norad_id": crossing.get("norad_id")}
        reasons.append(f"lies on the computed track of {crossing['name']}")
        kind, confidence = "satellite", "high"

    # 2. Speed: too fast for anything in orbit, or satellite pace.
    too_fast = slow_enough = False
    if rate is not None:
        mid = ((sky[0][0] + sky[1][0]) / 2.0, (sky[0][1] + sky[1][1]) / 2.0)
        alt = _elevation(mid[0], mid[1], exif_info, when_utc)
        max_rate = _sat_max_rate(90.0 if alt is None else alt)
        streak["sat_max_rate_deg_s"] = round(max_rate, 3)
        if alt is not None:
            streak["elevation_deg"] = round(alt, 1)
        too_fast = rate > max_rate
        slow_enough = SAT_MIN_RATE_DEG_S <= rate <= max_rate
        if too_fast:
            where = f"at {alt:.0f} degrees up" if alt is not None else "even overhead"
            reasons.append(
                f"{streak['length_deg']:.1f} degrees in a {exposure:g} s exposure "
                f"is {rate:.2f} degrees/s; no satellite covers more than "
                f"{max_rate:.2f} {where}")

    # 3. The light curve, read together with the speed. A flat trail that
    # is too fast for orbit is not a meteor either: an aircraft, or
    # something in the foreground the pixel tests let through.
    flat, taper = streak["flatness"], streak["taper_start"]
    meteor_shape = flat < METEOR_MAX_FLATNESS and taper >= METEOR_MIN_TAPER
    sat_shape = flat >= SATELLITE_MIN_FLATNESS and \
        max(taper, streak["taper_end"]) < SATELLITE_MAX_TAPER
    if meteor_shape:
        reasons.append("fades in, brightens along its path, and stops")
        if kind == "unknown":
            kind, confidence = "meteor", ("high" if too_fast else "medium")
    elif sat_shape:
        reasons.append("the same brightness end to end, cut off at both")
        if kind == "unknown" and too_fast:
            reasons.append("flat like a satellite yet too fast for one: an aircraft, perhaps")
        elif kind == "unknown":
            kind = "satellite"
            confidence = "medium" if slow_enough else "low"
            if slow_enough:
                reasons.append(
                    f"and {rate:.2f} degrees/s over the exposure is satellite pace")
    elif kind == "unknown":
        if too_fast:
            kind, confidence = "meteor", "low"
            reasons.append("brightness along the line is not a clean meteor curve")
        else:
            reasons.append("brightness along the line fits neither a meteor nor a satellite cleanly")
    if streak.get("dashed"):
        reasons.append("broken along its length, as a stacked night-mode exposure leaves moving things")
    if streak.get("periodic_breaks") and kind != "unknown" and confidence != "high":
        confidence = "low"
        reasons.append(
            f"broken at {streak['periodic_breaks']} regular intervals: the frame "
            "boundaries of a stacked exposure, so it moved steadily through several "
            "frames, which suits a satellite as well as a meteor")

    # 4. Meteors: which shower, if any.
    if kind == "meteor" and sky:
        shower = match_shower(sky[0], sky[1], when_utc)
        streak["shower"] = shower
        if shower:
            reasons.append(f"points back to the {shower['name']} radiant")
        elif when_utc is not None:
            reasons.append("points at no active shower's radiant: a sporadic")

    streak["kind"] = kind
    streak["confidence"] = confidence
    streak["reasons"] = reasons
    return streak


def annotate(image_path, wcs_path, width, height, exif_info, sats=None):
    """The streak layer for a solved image: {"streaks": [...], ...}.
    May raise (unreadable image); the worker treats it as best-effort."""
    found = detect(image_path)
    wcs = None
    if wcs_path:
        from astropy.io import fits
        from astropy.wcs import WCS

        with fits.open(wcs_path) as hdul:
            wcs = WCS(hdul[0].header)
    when_utc, _ = ephemeris.resolve_utc(exif_info)
    crossings = (sats or {}).get("crossings") or []
    for streak in found["streaks"]:
        classify(streak, wcs, width, exif_info, crossings, when_utc)
    return found
