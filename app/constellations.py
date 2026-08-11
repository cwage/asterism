"""Constellation stick figures: Stellarium's modern-skyculture line set
(pairs of Hipparcos star numbers) resolved to sky coordinates through the
HYG catalog, then projected through the solved WCS like everything else."""

import csv
import math
import os

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalogs")
LINES_FILE = "constellations.fab"

# IAU three-letter abbreviations, as used by the Stellarium line set.
NAMES = {
    "And": "Andromeda", "Ant": "Antlia", "Aps": "Apus", "Aql": "Aquila",
    "Aqr": "Aquarius", "Ara": "Ara", "Ari": "Aries", "Aur": "Auriga",
    "Boo": "Boötes", "Cae": "Caelum", "Cam": "Camelopardalis",
    "Cap": "Capricornus", "Car": "Carina", "Cas": "Cassiopeia",
    "Cen": "Centaurus", "Cep": "Cepheus", "Cet": "Cetus",
    "Cha": "Chamaeleon", "Cir": "Circinus", "CMa": "Canis Major",
    "CMi": "Canis Minor", "Cnc": "Cancer", "Col": "Columba",
    "Com": "Coma Berenices", "CrA": "Corona Australis",
    "CrB": "Corona Borealis", "Crt": "Crater", "Cru": "Crux",
    "Crv": "Corvus", "CVn": "Canes Venatici", "Cyg": "Cygnus",
    "Del": "Delphinus", "Dor": "Dorado", "Dra": "Draco", "Equ": "Equuleus",
    "Eri": "Eridanus", "For": "Fornax", "Gem": "Gemini", "Gru": "Grus",
    "Her": "Hercules", "Hor": "Horologium", "Hya": "Hydra",
    "Hyi": "Hydrus", "Ind": "Indus", "Lac": "Lacerta", "Leo": "Leo",
    "Lep": "Lepus", "Lib": "Libra", "LMi": "Leo Minor", "Lup": "Lupus",
    "Lyn": "Lynx", "Lyr": "Lyra", "Men": "Mensa", "Mic": "Microscopium",
    "Mon": "Monoceros", "Mus": "Musca", "Nor": "Norma", "Oct": "Octans",
    "Oph": "Ophiuchus", "Ori": "Orion", "Pav": "Pavo", "Peg": "Pegasus",
    "Per": "Perseus", "Phe": "Phoenix", "Pic": "Pictor",
    "PsA": "Piscis Austrinus", "Psc": "Pisces", "Pup": "Puppis",
    "Pyx": "Pyxis", "Ret": "Reticulum", "Scl": "Sculptor",
    "Sco": "Scorpius", "Sct": "Scutum", "Ser": "Serpens", "Sex": "Sextans",
    "Sge": "Sagitta", "Sgr": "Sagittarius", "Tau": "Taurus",
    "Tel": "Telescopium", "TrA": "Triangulum Australe",
    "Tri": "Triangulum", "Tuc": "Tucana", "UMa": "Ursa Major",
    "UMi": "Ursa Minor", "Vel": "Vela", "Vir": "Virgo", "Vol": "Volans",
    "Vul": "Vulpecula",
}

_lines_cache = None


def load_lines():
    """Parsed line set: [{"abbr", "name", "segments": [((ra1, dec1),
    (ra2, dec2)), ...]}, ...] with coordinates in degrees. Returns [] if
    the line file hasn't been fetched; pairs whose stars are missing from
    HYG are dropped silently (a handful of dim southern stars)."""
    global _lines_cache
    if _lines_cache is not None:
        return _lines_cache
    path = os.path.join(CATALOG_DIR, LINES_FILE)
    if not os.path.exists(path):
        # Don't cache the miss: the file may get fetched while we're running,
        # and the other data files recover without a restart too.
        return []

    raw = []  # (abbr, [(hip_a, hip_b), ...])
    wanted = set()
    with open(path) as f:
        for line in f:
            parts = line.split()
            # Format: "Ori 17 hip hip hip hip ..." — abbr, pair count, pairs.
            if len(parts) < 4 or not parts[1].isdigit():
                continue
            hips = [int(p) for p in parts[2:] if p.isdigit()]
            pairs = list(zip(hips[0::2], hips[1::2]))
            raw.append((parts[0], pairs))
            wanted.update(hips)

    pos = _hip_positions(wanted)
    figures = []
    for abbr, pairs in raw:
        segments = [(pos[a], pos[b]) for a, b in pairs
                    if a in pos and b in pos]
        if segments:
            figures.append({"abbr": abbr, "name": NAMES.get(abbr, abbr),
                            "segments": segments})
    _lines_cache = figures
    return figures


def _hip_positions(wanted):
    """Hipparcos number -> (ra_deg, dec_deg) from HYG. RA is stored in
    hours; convert to degrees."""
    path = os.path.join(CATALOG_DIR, "hyg.csv")
    pos = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                hip = int(row["hip"])
            except (KeyError, ValueError, TypeError):
                continue
            if hip in wanted and hip not in pos:
                try:
                    pos[hip] = (float(row["ra"]) * 15.0, float(row["dec"]))
                except (KeyError, ValueError):
                    continue
    return pos


def annotate(wcs_path, width, height):
    """Project the stick figures through a solved WCS. Returns
    [{"name", "abbr", "segments": [[x1, y1, x2, y2], ...]}] keeping only
    segments with at least one endpoint on the frame, so partial figures
    at the edges still draw."""
    from astropy.io import fits
    from astropy.wcs import WCS

    figures = load_lines()
    if not figures:
        return []

    with fits.open(wcs_path) as hdul:
        wcs = WCS(hdul[0].header)

    pix = {}

    def project(radec):
        if radec not in pix:
            try:
                x, y = wcs.all_world2pix(radec[0], radec[1], 0)
                x, y = float(x), float(y)
                pix[radec] = (x, y) if math.isfinite(x) and math.isfinite(y) else None
            except Exception:
                # SIP inversion can fail to converge for points far off-frame.
                pix[radec] = None
        return pix[radec]

    # TAN projection sends the far hemisphere to absurd pixel coordinates;
    # cap how far off-frame the outer endpoint of a kept segment may sit.
    lim = 2.0 * max(width, height)
    out = []
    for fig in figures:
        segments = []
        for a, b in fig["segments"]:
            pa, pb = project(a), project(b)
            if pa is None or pb is None:
                continue
            in_a = 0 <= pa[0] < width and 0 <= pa[1] < height
            in_b = 0 <= pb[0] < width and 0 <= pb[1] < height
            if not (in_a or in_b):
                continue
            if max(abs(pa[0]), abs(pa[1]), abs(pb[0]), abs(pb[1])) > lim:
                continue
            segments.append([round(pa[0], 1), round(pa[1], 1),
                             round(pb[0], 1), round(pb[1], 1)])
        if segments:
            out.append({"name": fig["name"], "abbr": fig["abbr"],
                        "segments": segments})
    return out
