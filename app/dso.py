"""Naked-eye deep-sky objects (#16): the handful of named/Messier objects a
phone night mode can plausibly catch, projected through the solved WCS like
the star catalog. Positions and sizes from the HYG repo's dso.csv.

These are extended, diffuse objects, so verification treats them like the
Moon and planets: warp-corrected but never snapped to a point source or
declared cloud-hidden (kind != "star" -> status "projected")."""

import csv
import os

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")

MAG_LIMIT = 4.5
# Dark nebulae (labeling an absence) and stray star records aren't DSOs a
# label helps with.
SKIP_TYPES = {"DN", "*"}
# dso.csv leaves the household names blank; supply them for the marquee
# objects so the label answers the actual question ("what's that smudge?").
DISPLAY_NAMES = {
    "M 31": "Andromeda Galaxy (M31)",
    "M 42": "Orion Nebula (M42)",
    "M 45": "Pleiades (M45)",
    "M 44": "Beehive Cluster (M44)",
    "M 6": "Butterfly Cluster (M6)",
    "M 7": "Ptolemy Cluster (M7)",
}

_catalog_cache = None


def load_catalog():
    """Bright (mag <= 4.5) DSOs that are properly named or Messier-numbered,
    brightest first. Anonymous Collinder/NGC groups at these magnitudes are
    loose star fields the star labels already cover — a "Col 135" label
    would answer nothing. RA is stored in hours, like HYG."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    path = os.path.join(CATALOG_DIR, "dso.csv")
    objects = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("type") or "").strip() in SKIP_TYPES:
                continue
            try:
                mag = float(row["mag"])
                ra = float(row["ra"]) * 15.0
                dec = float(row["dec"])
            except (KeyError, ValueError):
                continue
            if mag > MAG_LIMIT:
                continue
            name = (row.get("name") or "").strip()
            cat = (row.get("cat1") or "").strip()
            cat_id = f"{cat} {(row.get('id1') or '').strip()}"
            if cat_id in DISPLAY_NAMES:
                name = DISPLAY_NAMES[cat_id]
            elif not name:
                if cat != "M":
                    continue
                name = cat_id
            try:
                # r1 is the major-axis diameter in arcminutes.
                radius_deg = float(row["r1"]) / 60.0 / 2.0
            except (KeyError, ValueError):
                radius_deg = None
            objects.append({"name": name, "ra": ra, "dec": dec, "mag": mag,
                            "radius_deg": radius_deg})
    objects.sort(key=lambda o: o["mag"])
    _catalog_cache = objects
    return objects


def annotate(wcs_path, width, height, max_labels=8):
    """Project catalog DSOs through the solved WCS; return pixel-space labels
    with kind "dso" and, when the catalog has a size, an on-image radius so
    the UI can circle the object's actual extent."""
    from astropy.io import fits
    from astropy.wcs import WCS
    from astropy.wcs.utils import proj_plane_pixel_scales

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)
    deg_per_px = float(min(proj_plane_pixel_scales(wcs)))

    labels = []
    for obj in load_catalog():
        try:
            x, y = wcs.all_world2pix(obj["ra"], obj["dec"], 0)
            x, y = float(x), float(y)
        except Exception:
            continue
        if not (0 <= x < width and 0 <= y < height):
            continue
        label = {"name": obj["name"], "x": round(x, 1), "y": round(y, 1),
                 "mag": obj["mag"], "kind": "dso"}
        if obj["radius_deg"] and deg_per_px > 0:
            label["radius_px"] = round(obj["radius_deg"] / deg_per_px, 1)
        labels.append(label)
        if len(labels) >= max_labels:
            break
    return labels
