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
"""

import numpy as np
from PIL import Image

# Tunables as fractions of image width, so they track resolution.
SEARCH_RADIUS_FRAC = 0.025  # how far a stack can plausibly drag a star
SNAP_RADIUS_FRAC = 0.006    # how close a source must be after correction
WARP_FLAG_FRAC = 0.005      # p90 correction above this flags a warped image
MIN_FIELD_MATCHES = 4       # below this, fall back to a constant shift


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
    (field, n_used) where field(x, y) -> (dx, dy). One sigma-clip round
    drops mismatches (e.g. a hidden star whose nearest peak is a neighbor)
    before the final fit."""
    if not matches:
        return (lambda x, y: (0.0, 0.0)), 0
    m = np.asarray(matches, dtype=float)
    if len(m) < MIN_FIELD_MATCHES:
        sx, sy = float(np.median(m[:, 2])), float(np.median(m[:, 3]))
        return (lambda x, y: (sx, sy)), len(m)

    pts = m[:, :2] / norm
    for _ in range(2):
        cx = _tps_solve(pts, m[:, 2], lam)
        cy = _tps_solve(pts, m[:, 3], lam)
        rx = _tps_eval(cx, pts, pts) - m[:, 2]
        ry = _tps_eval(cy, pts, pts) - m[:, 3]
        r = np.hypot(rx, ry)
        keep = r < max(3.0 * 1.4826 * np.median(r), 3.0)
        if keep.all() or keep.sum() < MIN_FIELD_MATCHES:
            break
        m, pts = m[keep], pts[keep]

    def field(x, y):
        at = np.array([[x / norm, y / norm]])
        return (float(_tps_eval(cx, pts, at)[0]),
                float(_tps_eval(cy, pts, at)[0]))

    return field, len(m)


def apply(image_path, labels, figures):
    """Verify and correct labels/figures against the image. Returns
    (labels, figures, meta). Never raises on a bad image: the originals
    come back with meta["verified"] = False."""
    try:
        img = np.asarray(Image.open(image_path).convert("L"), dtype=np.float32)
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
            nearest = min(peaks, key=lambda p: (p[0] - lab["x"]) ** 2 +
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
    corrections = []
    hidden = matched = 0
    for lab, peaks in zip(labels, peaks_by_label):
        lab = dict(lab)
        dx, dy = field_at(lab["x"], lab["y"])
        cx, cy = lab["x"] + dx, lab["y"] + dy
        if peaks is None:  # moon/planet: correct for warp, nothing to snap
            lab["status"] = "projected"
            lab["x"], lab["y"] = round(cx, 1), round(cy, 1)
        else:
            near = [p for p in peaks
                    if (p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= snap_r ** 2]
            if near:
                best = max(near, key=lambda p: p[2])
                corrections.append(np.hypot(best[0] - lab["x"],
                                            best[1] - lab["y"]))
                lab["x"], lab["y"] = round(best[0], 1), round(best[1], 1)
                lab["status"] = "matched"
                matched += 1
            else:
                lab["x"], lab["y"] = round(cx, 1), round(cy, 1)
                lab["status"] = "hidden"
                hidden += 1
        out.append(lab)

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
    meta = {
        "verified": True,
        "stars_matched": matched,
        "stars_hidden": hidden,
        "field_matches": n_used,
        "median_correction_px": round(float(np.median(corrections)), 1)
        if corrections else 0.0,
        "p90_correction_px": round(p90, 1),
        "warped": bool(p90 > max(6.0, width * WARP_FLAG_FRAC)),
    }
    return out, out_figures, meta
