"""Star data for the fly-around page: every HYG star as a point in space.

The page (static/fly/) backs away from Earth and looks at a solved
photo's stars from outside, so it needs what the solver never does: where
each star is in three dimensions. HYG carries that (x, y, z in parsecs,
from the parallaxes), and this packs it into the two files the page loads:

  stars.bin     little-endian float32, five per star: x, y, z, absolute
                magnitude, B-V. Brightest first as seen from Earth, so a
                small index is a bright star and index 0 is the Sun.
  catalog.json  how many stars; the index of every named one; which of
                those names are proper names; the constellation figures
                as pairs of indices; and a version for stars.bin, which
                the page puts in that file's URL so it can be cached for
                good.

A solve's labels carry a name and a pixel position and nothing else, so
the name is what joins a label to a star here. The names are the solver's
own (solver.star_name), not a second copy of the rule.

Stars HYG has no usable parallax for sit at its placeholder distance of
100000 pc. They are kept as they are: at that distance they do not move
on any trip the page takes, which is the right behaviour for a star whose
distance is unknown, and their absolute magnitude is derived from the
same placeholder, so they are as bright from Earth as they should be.

Built on first request and kept under DATA_DIR/fly, like the TLE cache:
parsing 120,000 rows takes a second or two, and the catalog only changes
when the image is rebuilt.
"""

import csv
import hashlib
import json
import os
import struct
import threading

from . import constellations, db, solver

# Bump when the files' layout changes, so a cache built by older code is
# not served to a page that expects the new one.
LAYOUT = 1

# B-V for a star with none recorded: roughly a G dwarf, so it draws white.
DEFAULT_CI = 0.65

_lock = threading.Lock()


def _stars(path):
    stars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                star = {
                    "mag": float(row["mag"]),
                    "absmag": float(row["absmag"]),
                    "xyz": (float(row["x"]), float(row["y"]), float(row["z"])),
                }
            except (KeyError, ValueError):
                continue
            try:
                star["ci"] = float(row["ci"])
            except (KeyError, ValueError):
                star["ci"] = DEFAULT_CI
            # The Sun is home: the page stands the camera there and needs
            # to know which row it is. Nothing in a solve is labelled "Sun"
            # (the solver skips the row), so the name cannot collide.
            sun = (row.get("proper") or "").strip() == "Sol"
            star["name"] = "Sun" if sun else solver.star_name(row)
            star["proper"] = sun or bool((row.get("proper") or "").strip())
            hip = (row.get("hip") or "").strip()
            star["hip"] = int(hip) if hip.isdigit() else None
            stars.append(star)
    stars.sort(key=lambda s: s["mag"])
    return stars


def _figures(path, hip_index):
    """Stellarium's line set as {abbr: [index, index, ...]}, consecutive
    pairs being segments. A pair with a star HYG lacks is dropped, as
    constellations.load_lines drops it."""
    figures = {}
    if not os.path.exists(path):
        return figures
    with open(path) as f:
        for line in f:
            parts = line.split()
            # Format: "Ori 17 hip hip hip hip ..." — abbr, pair count, pairs.
            if len(parts) < 4 or not parts[1].isdigit():
                continue
            hips = [int(p) for p in parts[2:] if p.isdigit()]
            pairs = []
            for a, b in zip(hips[0::2], hips[1::2]):
                if a in hip_index and b in hip_index:
                    pairs += [hip_index[a], hip_index[b]]
            figures[parts[0]] = pairs
    return figures


def build(catalog_dir):
    """(stars.bin as bytes, catalog.json as a dict) from the catalogs in
    `catalog_dir`."""
    stars = _stars(os.path.join(catalog_dir, "hyg.csv"))
    names, proper, hip_index = {}, [], {}
    for i, star in enumerate(stars):
        # Brightest first, so a name two rows share goes to the brighter.
        if star["name"] and star["name"] not in names:
            names[star["name"]] = i
            if star["proper"]:
                proper.append(star["name"])
        if star["hip"] is not None:
            hip_index.setdefault(star["hip"], i)
    packed = b"".join(
        struct.pack("<5f", *star["xyz"], star["absmag"], star["ci"]) for star in stars)
    catalog = {
        "count": len(stars),
        "names": names,
        "proper": proper,
        "lines": _figures(os.path.join(catalog_dir, constellations.LINES_FILE), hip_index),
    }
    return packed, catalog


def _signature(catalog_dir):
    """What the cache was built from: the layout version and each source
    file's size and modification time. None if the star catalog has not
    been fetched."""
    sources = []
    for name in ("hyg.csv", constellations.LINES_FILE):
        try:
            stat = os.stat(os.path.join(catalog_dir, name))
        except FileNotFoundError:
            if name == "hyg.csv":
                return None
            # No line set just means no figures; the miss is part of the
            # signature so that fetching it later rebuilds.
            sources.append([name, None, None])
            continue
        sources.append([name, stat.st_size, int(stat.st_mtime)])
    return {"layout": LAYOUT, "sources": sources}


def files():
    """Paths of (stars.bin, catalog.json), building them first if they are
    missing or the catalog has changed since they were built. None if the
    star catalog has not been fetched."""
    signature = _signature(solver.CATALOG_DIR)
    if signature is None:
        return None
    out = os.path.join(db.DATA_DIR, "fly")
    stars_path = os.path.join(out, "stars.bin")
    catalog_path = os.path.join(out, "catalog.json")
    built_path = os.path.join(out, "built.json")
    # Sync handlers run in a thread pool: two first requests must not both
    # build, and neither may be served a half-written file.
    with _lock:
        try:
            with open(built_path) as f:
                fresh = json.load(f) == signature
        except (FileNotFoundError, ValueError):
            fresh = False
        if not (fresh and os.path.exists(stars_path) and os.path.exists(catalog_path)):
            packed, catalog = build(solver.CATALOG_DIR)
            catalog["version"] = hashlib.sha256(packed).hexdigest()[:12]
            os.makedirs(out, exist_ok=True)
            # The signature goes last: until it is there the files are not
            # trusted, so a crash part-way through means a rebuild.
            _replace(stars_path, packed, "wb")
            _replace(catalog_path, json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), "w")
            _replace(built_path, json.dumps(signature), "w")
    return stars_path, catalog_path


def _replace(path, data, mode):
    tmp = path + ".tmp"
    with open(tmp, mode) as f:
        f.write(data)
    os.replace(tmp, path)
