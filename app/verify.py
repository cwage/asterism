"""Post-solve verification: close the loop between projected labels and
the actual pixels.

Phone night modes are computational stacks, and their tile-based
align-and-merge can lock onto moving clouds, dragging the stars in those
tiles up to ~1 degree away from where the global WCS puts them (issue #28).
Clouds also hide stars outright, leaving labels floating over blank sky.
So instead of trusting the projection blindly: detect the actual source
near each predicted position, fit a smooth residual field over the
confident matches, re-place every label through that field, then snap
each star label to its source — or mark it hidden when nothing is there.

DSOs are extended, so they are never snapped to a point source; instead
their pixels are checked photometrically (#50) — core aperture against a
surrounding annulus, or resolved member stars for clusters — and a DSO
with no measurable signal at its position is marked hidden too.
"""

import math

import numpy as np
from PIL import Image

# Tunables as fractions of image width, so they track resolution.
SEARCH_RADIUS_FRAC = 0.025  # how far a stack can plausibly drag a star
SNAP_RADIUS_FRAC = 0.006    # how close a source must be after correction
WARP_FLAG_FRAC = 0.005      # p90 correction above this flags a warped image
MIN_FIELD_MATCHES = 4       # below this, fall back to a constant shift
CANDIDATE_AMP_FRAC = 0.25   # ignore match peaks far dimmer than the window's best
BRIGHT_MAG = 1.5            # stars at least this bright get the amplitude sanity check

# DSO photometric check (#50): core aperture vs. surrounding annulus, both
# as multiples of the object's catalog extent radius.
DSO_CORE_FRAC = 0.5
DSO_ANNULUS = (1.6, 2.6)
DSO_MIN_EXCESS = 1.5        # ADU floor — denoised night modes can have near-zero MAD
DSO_NOISE_FRAC = 0.5        # ...otherwise demand this fraction of the local pixel noise
DSO_DEFAULT_RADIUS_FRAC = 0.008  # aperture when the catalog has no size
# Open clusters are resolved star groups, not diffuse glow: a median over
# the core would reject a perfectly visible Pleiades. They pass on detected
# point sources too.
DSO_CLUSTER_TYPES = {"OC", "OC+Neb", "Ast", "MWSC"}
DSO_CLUSTER_MIN_PEAKS = 3

# Width the pre-solve star gate works at. Its isolation test is in fixed
# pixels, so bigger uploads are scaled down to meet it (see count_stars).
GATE_WIDTH = 1600

# Limiting magnitude (#122): the deep catalog's detection rate per
# half-magnitude bin, read off where it falls through DEPTH_DETECT_FRAC.
DEPTH_BIN_MAG = 0.5
DEPTH_MIN_PER_BIN = 6       # fewer stars than this and a bin says nothing
DEPTH_MAX_PER_BIN = 120     # more than this is cost without information
DEPTH_DETECT_FRAC = 0.5
DEPTH_CONTROL_MAX = 0.3     # chance hits at star-free spots above this: no estimate
DEPTH_WINDOW_MIN_PX = 12    # detection window radius floor, in pixels
# The depth test accepts a source far closer than the label snap does.
# Within SNAP_RADIUS_FRAC (21px, a third of a degree, on a 3472px frame)
# the sky itself has a catalog star to magnitude 8 more often than not:
# a real Pixel 8a frame measured a 62% hit rate at star-free control
# spots. At 0.002 the chance rate is a tenth of that. What the tight
# radius costs is stars whose corrected position is off by more than a
# few pixels, which biases the answer shallower — the honest direction.
DEPTH_SNAP_FRAC = 0.002
DEPTH_SNAP_MIN_PX = 4.0
DEPTH_SNAP_RESID = 1.5      # ...or this many times the bright stars' own snap residual
DEPTH_SNAP_MAX_FRAC = 0.005 # never wider than this, whatever the residual says
DEPTH_STOP_MARGIN = 0.15    # raw rate this close to chance, twice running: stop walking
# The threshold is relative to how many of the *bright* stars are found:
# a treeline over a third of the frame, or a cloud bank, hides a third of
# every bin alike, and must not read as the sky running out at magnitude
# 3. Below DEPTH_BASELINE_MIN even the bright stars are mostly missing —
# foreground, cloud, a warped field the fit didn't follow — and the
# method has nothing honest to say. 0.45 admits the common half-sky,
# half-garden composition (measured at 0.49-0.52 on a greenhouse scene
# with trees across the bottom 45% of the frame).
DEPTH_BASELINE_MAG = 4.5    # bins to this centre define the bright end
DEPTH_BASELINE_MIN = 0.45


def count_stars(image_path, grid=24, thr_sigma=5.0, min_amp=12.0,
                max_count=500):
    """Fast whole-frame count of star-like sources, for the pre-solve gate.

    A star is a compact peak whose surroundings fall away: bright but
    non-isolated maxima (cloud texture, foliage gaps, kitchen-table glare)
    are rejected by requiring the annulus 4-10 px out to sit well below
    the peak. Coarse block-median background handles sky gradients. The
    gate stays permissive — its job is rejecting zero-star images, not
    predicting solve success. Returns None if the image can't be read.

    That annulus is measured in pixels, so the whole detector is only
    valid at one scale, and the image is normalized to it first. Measured
    2026-08-14 on real Pixel 9 astro shots: the same photo counts 0 stars
    at its native 4000px width and 67 at 1600px, because night-mode
    stacking leaves a star's glow still bright 10px out on a 12MP frame,
    failing the isolation test for every real star. Those photos were
    rejected as "not a sky photo" despite plate-solving in under 6
    seconds. Downscaling also makes the gate cheaper on big uploads."""
    try:
        # Context-managed: this runs for every non-deep upload, so the file
        # handle has to close on the spot rather than whenever GC notices.
        with Image.open(image_path) as src:
            img = src.convert("L")
            if img.width > GATE_WIDTH:
                img = img.resize(
                    (GATE_WIDTH,
                     max(1, round(img.height * GATE_WIDTH / img.width))),
                    Image.LANCZOS)
            img = np.asarray(img, dtype=np.float32)
    except Exception:
        return None
    h, w = img.shape
    bh, bw = max(8, h // grid), max(8, w // grid)
    H, W = (h // bh) * bh, (w // bw) * bw
    a = img[:H, :W]
    bg = np.kron(np.median(a.reshape(H // bh, bh, W // bw, bw), axis=(1, 3)),
                 np.ones((bh, bw), dtype=np.float32))
    det = a - bg
    # Center before the MAD: the block-median background can leave a small
    # global offset that would otherwise bias the threshold.
    mad = np.median(np.abs(det - np.median(det)))
    thr = max(thr_sigma * 1.4826 * mad, min_amp)
    # Strict local maxima over 8 neighbors; a tiny position-dependent
    # dither breaks the exact ties of saturated flat-topped stars.
    det = det + (np.arange(H * W, dtype=np.float32).reshape(H, W) % 7) * 1e-4
    neigh = np.full_like(det, -np.inf)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == dx == 0:
                continue
            shifted = np.roll(np.roll(det, dy, 0), dx, 1)
            np.maximum(neigh, shifted, out=neigh)
    cand = (det > thr) & (det > neigh)
    cand[:10, :] = cand[-10:, :] = False
    cand[:, :10] = cand[:, -10:] = False

    ys, xs = np.where(cand)
    order = np.argsort(det[ys, xs])[::-1][:4000]
    count = 0
    for i in order:
        y, x = int(ys[i]), int(xs[i])
        amp = det[y, x]
        ring = det[y - 10:y + 11, x - 10:x + 11].copy()
        ring[7:14, 7:14] = -np.inf  # mask the peak's own core
        if ring.max() < max(0.35 * amp, thr):
            count += 1
            if count >= max_count:
                break
    return count


def _detect_peaks(win, thr_sigma=5.0, min_amp=12.0, max_peaks=40):
    """Local maxima above the window's robust background, with subpixel
    centroids. Returns [(x, y, amplitude)] in window coordinates."""
    det = win - np.median(win)
    mad = np.median(np.abs(det))
    thr = max(thr_sigma * 1.4826 * mad, min_amp)
    ys, xs = np.where(det > thr)
    if len(xs) == 0:
        return []
    order = np.argsort(det[ys, xs])[::-1][:400]
    h, w = det.shape
    peaks = []
    for i in order:
        y, x = int(ys[i]), int(xs[i])
        y0, y1 = max(0, y - 3), min(h, y + 4)
        x0, x1 = max(0, x - 3), min(w, x + 4)
        patch = det[y0:y1, x0:x1]
        if det[y, x] < patch.max():
            continue
        # skip pixels within an already-kept peak's footprint
        if any((y - py) ** 2 + (x - px) ** 2 < 36 for px, py, _ in peaks):
            continue
        pos = np.clip(patch, 0, None)
        gy, gx = np.mgrid[y0:y1, x0:x1]
        total = pos.sum()
        peaks.append(((pos * gx).sum() / total, (pos * gy).sum() / total,
                      float(det[y, x])))
        if len(peaks) >= max_peaks:
            break
    return peaks


def _peaks_near(img, x, y, radius):
    """Detected sources within `radius` of (x, y), in image coordinates."""
    h, w = img.shape
    x0, x1 = int(max(0, x - radius)), int(min(w, x + radius + 1))
    y0, y1 = int(max(0, y - radius)), int(min(h, y + radius + 1))
    if x1 - x0 < 9 or y1 - y0 < 9:
        return []
    return [(px + x0, py + y0, amp)
            for px, py, amp in _detect_peaks(img[y0:y1, x0:x1])]


def _tps_solve(pts, vals, lam):
    """Thin-plate spline coefficients for scattered 2D data (one component).
    pts are pre-normalized to ~[0, 1]."""
    n = len(pts)
    d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        K = 0.5 * d2 * np.log(d2)  # r^2 log r
    K[~np.isfinite(K)] = 0.0
    P = np.hstack([np.ones((n, 1)), pts])
    A = np.zeros((n + 3, n + 3))
    A[:n, :n] = K + lam * np.eye(n)
    A[:n, n:] = P
    A[n:, :n] = P.T
    b = np.zeros(n + 3)
    b[:n] = vals
    return np.linalg.lstsq(A, b, rcond=None)[0]


def _tps_eval(coef, pts, at):
    d2 = ((at[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        K = 0.5 * d2 * np.log(d2)
    K[~np.isfinite(K)] = 0.0
    n = len(pts)
    return K @ coef[:n] + coef[n] + at @ coef[n + 1:]


def _fit_field(matches, norm, lam=1e-3):
    """Smooth displacement field from (x, y, dx, dy) matches. Returns
    (field, n_used) where field(x, y) -> (dx, dy).

    Outliers (a cloud blob matched instead of the real star) are rejected
    against a sigma-clipped affine fit first: a flexible TPS would bend
    itself through its own outliers and hide them, a plane can't. The TPS
    is then fitted to the affine's inliers only."""
    if not matches:
        return (lambda x, y: (0.0, 0.0)), 0
    m = np.asarray(matches, dtype=float)
    if len(m) < MIN_FIELD_MATCHES:
        sx, sy = float(np.median(m[:, 2])), float(np.median(m[:, 3]))
        return (lambda x, y: (sx, sy)), len(m)

    pts = m[:, :2] / norm
    A = np.hstack([np.ones((len(m), 1)), pts])
    keep = np.ones(len(m), dtype=bool)
    for _ in range(3):
        ax = np.linalg.lstsq(A[keep], m[keep, 2], rcond=None)[0]
        ay = np.linalg.lstsq(A[keep], m[keep, 3], rcond=None)[0]
        r = np.hypot(A @ ax - m[:, 2], A @ ay - m[:, 3])
        new = r < max(3.0 * 1.4826 * np.median(r[keep]), 4.0)
        if new.sum() < MIN_FIELD_MATCHES or (new == keep).all():
            break
        keep = new

    m, pts = m[keep], pts[keep]
    cx = _tps_solve(pts, m[:, 2], lam)
    cy = _tps_solve(pts, m[:, 3], lam)

    def field(x, y):
        at = np.array([[x / norm, y / norm]])
        return (float(_tps_eval(cx, pts, at)[0]),
                float(_tps_eval(cy, pts, at)[0]))

    return field, len(m)


def _source_within(img, x, y, snap_r, win_r):
    """Whether a detected peak sits within snap_r of (x, y)."""
    for px, py, _ in _peaks_near(img, x, y, win_r):
        if (px - x) ** 2 + (py - y) ** 2 <= snap_r ** 2:
            return True
    return False


def _limiting_magnitude(img, deep, field_at, resid_p90=0.0):
    """How faint the photo reaches (#122). The deep catalog [(x, y, mag)]
    is tested bin by bin, bright to faint, for a detected source at each
    star's warp-corrected position; the limit is where the detection rate
    falls through DEPTH_DETECT_FRAC of the bright-end rate, interpolated
    between bin centres.

    Every test is paired with a control at a star-free offset. The chance
    rate that measures is taken out of every bin before anything is read,
    and a frame where any spot has a peak nearby (noise, JPEG blocking, a
    crowded field) reports no limit rather than a flattering one. The
    acceptance radius follows the precision the frame's own bright stars
    snapped with (`resid_p90`), so a warped frame whose fit is loose is
    tested loosely, and the control still says what that costs. The walk
    stops once two bins running detect no more than chance would."""
    h, w = img.shape
    snap_r = min(max(DEPTH_SNAP_MIN_PX, w * DEPTH_SNAP_FRAC,
                     DEPTH_SNAP_RESID * resid_p90),
                 max(DEPTH_SNAP_MIN_PX, w * DEPTH_SNAP_MAX_FRAC))
    win_r = max(DEPTH_WINDOW_MIN_PX, 2.0 * snap_r)
    bins = {}
    for x, y, mag in deep:
        bins.setdefault(math.floor(mag / DEPTH_BIN_MAG), []).append((x, y))
    # The catalog's own cut leaves its last bin partial; a half-empty bin
    # would misplace the "catalog ran out" edge, so it is not walked.
    faintest = max(mag for _, _, mag in deep)
    curve = []
    control_hits = control_n = 0
    quiet = 0
    for key in sorted(bins):
        if (key + 1) * DEPTH_BIN_MAG > faintest + 1e-9:
            break
        stars = bins[key]
        if len(stars) < DEPTH_MIN_PER_BIN:
            continue
        step = max(1, len(stars) // DEPTH_MAX_PER_BIN)
        hits = n = 0
        for x, y in stars[::step][:DEPTH_MAX_PER_BIN]:
            dx, dy = field_at(x, y)
            cx, cy = x + dx, y + dy
            if not (win_r <= cx < w - win_r and win_r <= cy < h - win_r):
                continue
            n += 1
            hits += _source_within(img, cx, cy, snap_r, win_r)
            ox = cx + 3 * win_r if cx + 4 * win_r < w else cx - 3 * win_r
            control_n += 1
            control_hits += _source_within(img, ox, cy, snap_r, win_r)
        if n < DEPTH_MIN_PER_BIN:
            continue
        frac = hits / n
        curve.append([round((key + 0.5) * DEPTH_BIN_MAG, 2), n, round(frac, 2)])
        running_control = control_hits / control_n
        quiet = quiet + 1 if frac - running_control <= DEPTH_STOP_MARGIN else 0
        if quiet >= 2:
            break
    control_frac = control_hits / control_n if control_n else 0.0
    out = {"limiting_mag": None, "curve": curve, "snap_px": round(snap_r, 1),
           "control_frac": round(control_frac, 2), "baseline": None,
           "catalog_limited": False}
    if not curve or control_frac > DEPTH_CONTROL_MAX:
        return out

    def excess(frac):
        # Chance hits inflate every bin alike: what a bin genuinely
        # detected is what's left once that rate is taken out.
        return max(0.0, (frac - control_frac) / (1.0 - control_frac))

    bright = [(n, f) for c, n, f in curve if c <= DEPTH_BASELINE_MAG]
    bright_n = sum(n for n, _ in bright)
    if bright_n < DEPTH_MIN_PER_BIN:
        return out
    baseline = excess(sum(n * f for n, f in bright) / bright_n)
    out["baseline"] = round(baseline, 2)
    if baseline < DEPTH_BASELINE_MIN:
        return out
    threshold = DEPTH_DETECT_FRAC * baseline
    fracs = [excess(f) for _, _, f in curve]
    # The trailing run of failing bins; its first member is the crossing.
    i = len(curve)
    while i > 0 and fracs[i - 1] < threshold:
        i -= 1
    if i == len(curve):
        # Never fell through: the catalog ran out before the photo did.
        out["limiting_mag"] = round(curve[-1][0] + DEPTH_BIN_MAG / 2, 1)
        out["catalog_limited"] = True
        return out
    if i == 0:
        return out  # even the brightest bin fails: nothing to read
    m1, m2 = curve[i - 1][0], curve[i][0]
    f1, f2 = fracs[i - 1], fracs[i]
    out["limiting_mag"] = round(
        m1 + (f1 - threshold) / (f1 - f2) * (m2 - m1), 1)
    return out


def _dso_glow_visible(img, x, y, r):
    """Extended-source check: median brightness inside DSO_CORE_FRAC * r
    against the DSO_ANNULUS ring, thresholded on the annulus's own noise.
    Medians keep field stars in either aperture from bending the answer,
    and a smooth sky gradient cancels between the symmetric apertures.
    Returns True/False, or None when too little of the aperture is
    on-frame to judge."""
    h, w = img.shape
    r_out = DSO_ANNULUS[1] * r
    x0, x1 = int(np.floor(x - r_out)), int(np.ceil(x + r_out)) + 1
    y0, y1 = int(np.floor(y - r_out)), int(np.ceil(y + r_out)) + 1
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - x, yy - y)
    win = img[y0:y1, x0:x1]
    core = win[d <= DSO_CORE_FRAC * r]
    ann = win[(d >= DSO_ANNULUS[0] * r) & (d <= r_out)]
    # Both apertures must be mostly on-frame, or the medians mean nothing.
    core_area = np.pi * (DSO_CORE_FRAC * r) ** 2
    ann_area = np.pi * (DSO_ANNULUS[1] ** 2 - DSO_ANNULUS[0] ** 2) * r ** 2
    if core.size < max(12, 0.5 * core_area) or ann.size < 0.5 * ann_area:
        return None
    med_ann = float(np.median(ann))
    noise = 1.4826 * float(np.median(np.abs(ann - med_ann)))
    excess = float(np.median(core)) - med_ann
    return excess >= max(DSO_MIN_EXCESS, DSO_NOISE_FRAC * noise)


def _dso_visible(img, lab, x, y, width):
    """Is there anything at the DSO's (warp-corrected) position? Cluster
    types pass on resolved member stars across their extent; everything
    (clusters included — an unresolved cluster is a glow patch) can pass
    on a core-over-annulus brightness excess. None means "couldn't
    judge"."""
    r = float(lab.get("radius_px") or width * DSO_DEFAULT_RADIUS_FRAC)
    r = min(max(r, 8.0), 0.25 * width)
    if lab.get("dso_type") in DSO_CLUSTER_TYPES:
        # _peaks_near searches a square window; keep only peaks truly
        # within the extent radius so corner field stars don't count.
        members = [p for p in _peaks_near(img, x, y, r)
                   if (p[0] - x) ** 2 + (p[1] - y) ** 2 <= r * r]
        if len(members) >= DSO_CLUSTER_MIN_PEAKS:
            return True
    return _dso_glow_visible(img, x, y, r)


def apply(image_path, labels, figures, deep=None):
    """Verify and correct labels/figures against the image. Returns
    (labels, figures, meta). Never raises on a bad image: the originals
    come back with meta["verified"] = False.

    `deep`, when given, is the full in-frame catalog as [(x, y, mag)]
    (solver.project_deep); meta then carries "depth", the limiting
    magnitude estimate (#122), measured with the same warp field.

    Assumes the WCS came from a star match: the displacement field is
    fitted by pairing each projected star with the nearest peak inside a
    search radius, which is only evidence if the projection is already
    close. Do not reuse this against a projection that has not been
    verified against the star field — every label finds *some* peak in a
    noisy night frame, and the fit is then noise.
    """
    try:
        with Image.open(image_path) as src:
            img = np.asarray(src.convert("L"), dtype=np.float32)
    except Exception:
        return labels, figures, {"verified": False, "error": "image unreadable"}

    height, width = img.shape
    search_r = max(40.0, width * SEARCH_RADIUS_FRAC)
    snap_r = max(8.0, width * SNAP_RADIUS_FRAC)

    peaks_by_label = []
    matches = []
    for lab in labels:
        if lab.get("kind") != "star":
            peaks_by_label.append(None)
            continue
        peaks = _peaks_near(img, lab["x"], lab["y"], search_r)
        peaks_by_label.append(peaks)
        if peaks:
            # Nearest *credible* peak: a faint blob a few px closer must
            # not out-compete the actual star (the Arcturus-vs-cloud-blob
            # failure) — drop candidates far dimmer than the window's best.
            amp_floor = CANDIDATE_AMP_FRAC * max(p[2] for p in peaks)
            cands = [p for p in peaks if p[2] >= amp_floor]
            nearest = min(cands, key=lambda p: (p[0] - lab["x"]) ** 2 +
                          (p[1] - lab["y"]) ** 2)
            matches.append((lab["x"], lab["y"],
                            nearest[0] - lab["x"], nearest[1] - lab["y"]))

    field, n_used = _fit_field(matches, float(max(width, height)))

    def field_at(x, y):
        # Sample at the nearest on-frame point: TPS extrapolation far
        # off-frame (constellation endpoints) is not to be trusted.
        return field(min(max(x, 0.0), width - 1.0),
                     min(max(y, 0.0), height - 1.0))

    out = []
    snaps = []  # (label, original_xy, corrected_xy, amp) for matched stars
    for lab, peaks in zip(labels, peaks_by_label):
        lab = dict(lab)
        dx, dy = field_at(lab["x"], lab["y"])
        cx, cy = lab["x"] + dx, lab["y"] + dy
        if peaks is None:  # moon/planet/DSO: correct for warp, nothing to snap
            lab["status"] = "projected"
            lab["x"], lab["y"] = round(cx, 1), round(cy, 1)
            # A DSO label must not circle empty sky-glow (#50): check the
            # pixels for extended-source signal at the corrected position.
            if lab.get("kind") == "dso" \
                    and _dso_visible(img, lab, cx, cy, width) is False:
                lab["status"] = "hidden"
        else:
            near = [p for p in peaks
                    if (p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= snap_r ** 2]
            if near:
                best = max(near, key=lambda p: p[2])
                snaps.append((lab, (lab["x"], lab["y"]), (cx, cy), best[2]))
                lab["x"], lab["y"] = round(best[0], 1), round(best[1], 1)
                lab["status"] = "matched"
            else:
                lab["x"], lab["y"] = round(cx, 1), round(cy, 1)
                lab["status"] = "hidden"
        out.append(lab)

    # Amplitude sanity for bright stars: a first-magnitude star "matched"
    # to a peak far dimmer than the frame's typical match is cloud noise
    # (or a star so dimmed by cloud that hidden is the honest answer).
    if snaps:
        med_amp = float(np.median([s[3] for s in snaps]))
        for lab, _, (cx, cy), amp in snaps:
            if lab["mag"] is not None and lab["mag"] <= BRIGHT_MAG \
                    and amp < CANDIDATE_AMP_FRAC * med_amp:
                lab["status"] = "hidden"
                lab["x"], lab["y"] = round(cx, 1), round(cy, 1)

    corrections = [np.hypot(lab["x"] - ox, lab["y"] - oy)
                   for lab, (ox, oy), _, _ in snaps
                   if lab["status"] == "matched"]
    matched = sum(1 for lab in out if lab.get("status") == "matched")
    hidden = sum(1 for lab in out if lab.get("status") == "hidden"
                 and lab.get("kind") == "star")
    dsos_hidden = sum(1 for lab in out if lab.get("status") == "hidden"
                      and lab.get("kind") == "dso")

    out_figures = []
    for fig in figures:
        segments = []
        for x1, y1, x2, y2 in fig["segments"]:
            d1 = field_at(x1, y1)
            d2 = field_at(x2, y2)
            segments.append([round(x1 + d1[0], 1), round(y1 + d1[1], 1),
                             round(x2 + d2[0], 1), round(y2 + d2[1], 1)])
        out_figures.append({**fig, "segments": segments})

    p90 = float(np.percentile(corrections, 90)) if corrections else 0.0
    depth = None
    if deep:
        # How precisely the field put the bright stars where they were
        # found sets how close the depth test demands a source to be.
        residuals = [np.hypot(lab["x"] - cx, lab["y"] - cy)
                     for lab, _, (cx, cy), _ in snaps
                     if lab["status"] == "matched"]
        resid_p90 = float(np.percentile(residuals, 90)) if residuals else 0.0
        depth = _limiting_magnitude(img, deep, field_at, resid_p90)
    meta = {
        "verified": True,
        "stars_matched": matched,
        "stars_hidden": hidden,
        "dsos_hidden": dsos_hidden,
        "field_matches": n_used,
        "median_correction_px": round(float(np.median(corrections)), 1)
        if corrections else 0.0,
        "p90_correction_px": round(p90, 1),
        "warped": bool(p90 > max(6.0, width * WARP_FLAG_FRAC)),
    }
    if depth is not None:
        meta["depth"] = depth
    return out, out_figures, meta
