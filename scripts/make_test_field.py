"""Render a synthetic star field from the HYG catalog — a fake 'phone photo'
with known contents for end-to-end pipeline testing.

Usage (in the container): python scripts/make_test_field.py /data/testfield.jpg
"""

import csv
import os
import sys

from astropy.wcs import WCS
from PIL import Image, ImageDraw

WIDTH, HEIGHT = 1200, 900
FOV_DEG = 40.0
CENTER_RA, CENTER_DEC = 84.0, 2.0  # Orion
MAG_LIMIT = 6.5

CATALOG = os.path.join(os.environ.get("CATALOG_DIR", "./catalogs"), "hyg.csv")


def main(out_path):
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [CENTER_RA, CENTER_DEC]
    wcs.wcs.crpix = [WIDTH / 2, HEIGHT / 2]
    scale = FOV_DEG / WIDTH
    wcs.wcs.cdelt = [-scale, scale]

    img = Image.new("L", (WIDTH, HEIGHT), 8)
    draw = ImageDraw.Draw(img)

    n = 0
    with open(CATALOG, newline="") as f:
        for row in csv.DictReader(f):
            try:
                mag = float(row["mag"])
                ra = float(row["ra"]) * 15.0
                dec = float(row["dec"])
            except (KeyError, ValueError):
                continue
            if mag > MAG_LIMIT or mag < -20:  # skip the Sun
                continue
            x, y = wcs.wcs_world2pix(ra, dec, 0)
            if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                continue
            r = max(1.0, 4.0 - mag * 0.5)
            b = int(min(255, 260 - mag * 30))
            draw.ellipse([x - r, y - r, x + r, y + r], fill=b)
            n += 1

    img.convert("RGB").save(out_path, quality=92)
    print(f"wrote {out_path}: {n} stars, {FOV_DEG} deg field at RA={CENTER_RA} Dec={CENTER_DEC}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/data/testfield.jpg")
