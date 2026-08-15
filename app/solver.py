"""Wrapper around solve-field: build scale hints from EXIF, run the solve,
project the bright-star catalog through the resulting WCS."""

import csv
import math
import os
import re
import subprocess
import time

INDEX_DIR = os.environ.get("ASTROMETRY_INDEX_DIR", "./indexes")
CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")
CPU_LIMIT = int(os.environ.get("SOLVE_CPULIMIT", "60"))

# Confidence floor for accepting a solve (#71). solve-field's exit code is not
# enough: when the CPU limit cuts a search short it can still write out the best
# hypothesis it had, and "best" can be a three-star triangle with two matching
# stars. That WCS points somewhere confidently wrong.
#
# Both numbers come from solve.match. LOGODDS is astrometry's natural-log odds
# that the match is real; astrometry's own --odds-to-solve default of 1e9 is
# log-odds 20.7, so 25 keeps our floor just above the library's while staying
# far below anything real.
#
# Measured 2026-08-15 on six featured production solves (five distinct fields;
# two of the files are re-encodes of one photo and solve identically) plus the
# two synthetic fixtures:
#
#     genuine solves    log-odds 93 - 825    17 - 192 matched stars
#     the #71 false WCS log-odds 9.5         2 matched stars
#
# Nothing observed lands between the two groups. The floors sit in that gap
# with room on both sides: 25 is well under the weakest real solve (93) and
# well over the false one, and 8 matched stars likewise (weakest real: 17).
DEFAULT_MIN_LOGODDS = 25.0
DEFAULT_MIN_MATCHES = 8
MIN_LOGODDS = float(os.environ.get("SOLVE_MIN_LOGODDS", DEFAULT_MIN_LOGODDS))
MIN_MATCHES = int(os.environ.get("SOLVE_MIN_MATCHES", DEFAULT_MIN_MATCHES))

_catalog_cache = None


def _write_cfg(out_dir):
    # List index files explicitly: autoindex tries every file in the dir and
    # logs a load failure for non-index files like .gitkeep (issue #17).
    cfg = os.path.join(out_dir, "astrometry.cfg")
    try:
        index_files = sorted(f for f in os.listdir(INDEX_DIR)
                             if f.endswith(".fits"))
    except OSError:
        index_files = []
    lines = ["inparallel"]
    if index_files:
        lines += [f"index {os.path.join(INDEX_DIR, f)}" for f in index_files]
    else:
        lines += [f"add_path {INDEX_DIR}", "autoindex"]
    with open(cfg, "w") as f:
        f.write("\n".join(lines) + "\n")
    return cfg


def match_stats(out_dir):
    """Match quality from solve-field's solve.match, or None if unreadable.

    Kept best-effort on purpose: a solve that wrote a WCS but no readable
    match table is accepted rather than thrown away, since the alternative is
    losing real solves to an astrometry output change."""
    path = os.path.join(out_dir, "solve.match")
    if not os.path.exists(path):
        return None
    try:
        from astropy.io import fits

        with fits.open(path) as hdul:
            row = hdul[1].data[0]
            return {"logodds": float(row["LOGODDS"]),
                    "nmatch": int(row["NMATCH"]),
                    "ndistract": int(row["NDISTRACT"])}
    except Exception:
        return None


def _clear_artifacts(out_dir):
    """Drop a previous attempt's outputs so this one can't inherit them.

    solve_tiered reuses one directory for every tier, and solve-field only
    writes solve.wcs when it solves. Before #71 that was harmless (the first
    success ended the loop), but a rejected low-confidence match now leaves a
    WCS on disk while the loop keeps going — and the next tier would be
    credited with it."""
    for name in ("solve.wcs", "solve.match", "solve.corr", "solve.rdls",
                 "solve.new", "solve-indx.xyls"):
        try:
            os.unlink(os.path.join(out_dir, name))
        except FileNotFoundError:
            pass


def solve(image_path, out_dir, fov_bounds):
    """Run solve-field. Returns dict with success, seconds, wcs_path, log tail.

    A solve-field exit code of 0 is necessary but not sufficient: the match has
    to clear MIN_LOGODDS/MIN_MATCHES as well (#71)."""
    os.makedirs(out_dir, exist_ok=True)
    _clear_artifacts(out_dir)
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

    stats = match_stats(out_dir) if solved else None
    low_confidence = bool(stats) and (stats["logodds"] < MIN_LOGODDS
                                      or stats["nmatch"] < MIN_MATCHES)
    if low_confidence:
        solved = False

    return {
        "success": solved,
        "seconds": round(seconds, 2),
        "wcs_path": wcs_path if solved else None,
        "fov_bounds": [round(low, 1), round(high, 1)],
        "log_tail": log_tail,
        "match": stats,
        "low_confidence": low_confidence,
    }


# Scale ranges tried in order when we have no (trustworthy) EXIF hint, or when
# the EXIF-derived range fails (crops keep focal length but shrink the field).
# Phone-typical widths first, then the cropped/zoomed range, then telephoto.
#
# The shipped indexes (4108-4119) reach narrower than they were specified for:
# measured 2026-08-14, synthetic fields solve in ~0.7s down to 2.5 deg and
# fail below that. A 10x phone periscope is ~8.6 deg and a 5x is ~17, so the
# telephoto tier is about photos whose EXIF didn't survive (screenshots,
# re-encodes) — with a focal length present, tier_plan hints the scale
# directly and never needs it. It runs last because a miss costs a full
# CPU_LIMIT, and only deep mode gets past the first tier.
FALLBACK_TIERS = [(30.0, 90.0), (8.0, 35.0), (2.5, 10.0)]

# Widest field the shipped indexes (4108-4119, quads to ~30 deg) can
# plausibly solve. Bench 2026-08-13 (#46): 0/9 ultrawide shots whose EXIF
# put the field at ~117 deg solved on any tier — their EXIF tier just
# burned ~70s each before the fallbacks got their (equally doomed) turn.
MAX_EXIF_FIELD = 110.0


def tier_plan(exif_info):
    """The scale tiers a full solve would try, in order: EXIF-derived
    bounds first when trustworthy, then the fallbacks (deduped).
    An EXIF tier implying a field wider than the indexes cover (#46,
    ultrawide lenses) is skipped — the fallbacks still run in case the
    EXIF was lying about the optics."""
    tiers = []
    f35 = exif_info.get("focal_35mm")
    if f35:
        fov = math.degrees(2 * math.atan(36.0 / (2 * f35)))
        if fov <= MAX_EXIF_FIELD:
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
        attempt = {
            "fov_bounds": result["fov_bounds"],
            "seconds": result["seconds"],
            "success": result["success"],
        }
        # Only recorded when there was a match to judge, so the common failure
        # (nothing matched at all) keeps its existing shape.
        if result.get("match"):
            attempt["match"] = result["match"]
            attempt["low_confidence"] = result["low_confidence"]
        attempts.append(attempt)
        if result["success"]:
            break
    result["attempts"] = attempts
    result["total_seconds"] = round(sum(a["seconds"] for a in attempts), 2)
    return result


# HYG's three-letter Greek abbreviations (`bayer` column) -> the letter itself.
GREEK_LETTERS = {
    "Alp": "α", "Bet": "β", "Gam": "γ", "Del": "δ", "Eps": "ε", "Zet": "ζ",
    "Eta": "η", "The": "θ", "Iot": "ι", "Kap": "κ", "Lam": "λ", "Mu": "μ",
    "Nu": "ν", "Xi": "ξ", "Omi": "ο", "Pi": "π", "Rho": "ρ", "Sig": "σ",
    "Tau": "τ", "Ups": "υ", "Phi": "φ", "Chi": "χ", "Psi": "ψ", "Ome": "ω",
}
_SUPERSCRIPTS = str.maketrans("123456789", "¹²³⁴⁵⁶⁷⁸⁹")


def _bayer_name(row):
    """Display name like "α Lup" or "γ² Vel" from HYG's `bayer`/`con`
    columns, or None when the row has no usable Bayer designation.
    hygdata_v41 writes component indices as "Gam-2"; accept "Gam2" too,
    since fetch-catalog.sh pulls an unpinned CURRENT csv that could drift."""
    bayer = (row.get("bayer") or "").strip()
    con = (row.get("con") or "").strip()
    if not bayer or not con:
        return None
    m = re.fullmatch(r"([A-Za-z]+)-?(\d?)", bayer)
    if not m:
        return None
    letter = GREEK_LETTERS.get(m.group(1))
    if not letter:
        return None
    return f"{letter}{m.group(2).translate(_SUPERSCRIPTS)} {con}"


def load_catalog():
    """HYG database rows for bright stars, brightest first: proper-named
    stars plus Bayer designations for the bright stars without one.
    HYG stores RA in hours; convert to degrees."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    path = os.path.join(CATALOG_DIR, "hyg.csv")
    stars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("proper") or "").strip()
            if name == "Sol":
                continue
            if not name:
                # Unnamed secondary components (Castor B and friends) would
                # duplicate their primary's designation at the same pixel.
                if (row.get("comp") or "1").strip() not in ("", "1"):
                    continue
                name = _bayer_name(row)
            if not name:
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
