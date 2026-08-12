"""Wrapper around solve-field: build scale hints from EXIF, run the solve,
project the bright-star catalog through the resulting WCS."""

import csv
import os
import subprocess
import time

INDEX_DIR = os.environ.get("ASTROMETRY_INDEX_DIR", "./indexes")
CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")
CPU_LIMIT = int(os.environ.get("SOLVE_CPULIMIT", "60"))

_catalog_cache = None


def _write_cfg(out_dir):
    cfg = os.path.join(out_dir, "astrometry.cfg")
    with open(cfg, "w") as f:
        f.write(f"add_path {INDEX_DIR}\nautoindex\ninparallel\n")
    return cfg


def solve(image_path, out_dir, fov_bounds):
    """Run solve-field. Returns dict with success, seconds, wcs_path, log tail."""
    os.makedirs(out_dir, exist_ok=True)
    cfg = _write_cfg(out_dir)
    low, high = fov_bounds
    cmd = [
        "solve-field",
        "--config", cfg,
        "--dir", out_dir,
        "--out", "solve",
        "--overwrite",
        "--no-plots",
        "--downsample", "2",
        "--cpulimit", str(CPU_LIMIT),
        "--scale-units", "degwidth",
        "--scale-low", f"{low:.2f}",
        "--scale-high", f"{high:.2f}",
        image_path,
    ]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    seconds = time.monotonic() - t0
    wcs_path = os.path.join(out_dir, "solve.wcs")
    solved = proc.returncode == 0 and os.path.exists(wcs_path)
    log_tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
    return {
        "success": solved,
        "seconds": round(seconds, 2),
        "wcs_path": wcs_path if solved else None,
        "fov_bounds": [round(low, 1), round(high, 1)],
        "log_tail": log_tail,
    }


# Scale ranges tried in order when we have no (trustworthy) EXIF hint, or when
# the EXIF-derived range fails (crops keep focal length but shrink the field).
# Phone-typical widths first, then the cropped/zoomed range. Anything narrower
# than ~5 degrees needs index files we don't ship in v1.
FALLBACK_TIERS = [(30.0, 90.0), (8.0, 35.0)]


def tier_plan(exif_info):
    """The scale tiers a full solve would try, in order: EXIF-derived
    bounds first when trustworthy, then the fallbacks (deduped)."""
    tiers = []
    if exif_info.get("focal_35mm"):
        tiers.append(tuple(exif_info["fov_bounds"]))
    for t in FALLBACK_TIERS:
        if not any(abs(t[0] - u[0]) < 2 and abs(t[1] - u[1]) < 2 for u in tiers):
            tiers.append(t)
    return tiers


def solve_tiered(image_path, out_dir, exif_info, tiers=None):
    """Run solve() over successively broader scale guesses until one sticks.
    Returns the last solve() result, with an `attempts` summary appended.
    `tiers` overrides the plan (the worker's quick/deep split)."""
    if tiers is None:
        tiers = tier_plan(exif_info)
    if not tiers:
        return {"success": False, "seconds": 0.0, "wcs_path": None,
                "fov_bounds": [0.0, 0.0], "log_tail": "no untried field scales",
                "attempts": [], "total_seconds": 0.0}

    attempts = []
    result = None
    for bounds in tiers:
        result = solve(image_path, out_dir, bounds)
        attempts.append({
            "fov_bounds": result["fov_bounds"],
            "seconds": result["seconds"],
            "success": result["success"],
        })
        if result["success"]:
            break
    result["attempts"] = attempts
    result["total_seconds"] = round(sum(a["seconds"] for a in attempts), 2)
    return result


def load_catalog():
    """HYG database rows for stars with proper names, brightest first.
    HYG stores RA in hours; convert to degrees."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    path = os.path.join(CATALOG_DIR, "hyg.csv")
    stars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("proper") or "").strip()
            if not name or name == "Sol":
                continue
            try:
                mag = float(row["mag"])
                ra = float(row["ra"]) * 15.0
                dec = float(row["dec"])
            except (KeyError, ValueError):
                continue
            if mag > 4.5:
                continue
            stars.append({"name": name, "ra": ra, "dec": dec, "mag": mag})
    stars.sort(key=lambda s: s["mag"])
    _catalog_cache = stars
    return stars


def annotate(wcs_path, width, height, max_labels=40):
    """Project catalog stars through the solved WCS; return pixel-space labels."""
    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)

    labels = []
    for star in load_catalog():
        try:
            x, y = wcs.all_world2pix(star["ra"], star["dec"], 0)
            x, y = float(x), float(y)
        except Exception:
            # SIP inversion can fail to converge for points far off-frame.
            continue
        if not (0 <= x < width and 0 <= y < height):
            continue
        labels.append({"name": star["name"], "x": round(x, 1),
                       "y": round(y, 1), "mag": star["mag"], "kind": "star"})
        if len(labels) >= max_labels:
            break
    return labels
