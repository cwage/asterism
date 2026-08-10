"""Solve-rate benchmark: run the solver over a directory of real photos.

Usage (in the container): python -m app.bench /photos
"""

import os
import sys
import tempfile

from . import exif, solver

EXTS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}


def main(photo_dir):
    photos = sorted(
        f for f in os.listdir(photo_dir)
        if os.path.splitext(f)[1].lower() in EXTS
    )
    if not photos:
        print(f"no images found in {photo_dir}")
        return

    solved = 0
    print(f"{'photo':<32} {'tiers tried':>18} {'solved':>7} {'seconds':>8}")
    for name in photos:
        path = os.path.join(photo_dir, name)
        try:
            info = exif.read_exif(path)
        except Exception as e:
            print(f"{name:<32} unreadable: {e}")
            continue
        with tempfile.TemporaryDirectory() as out_dir:
            result = solver.solve_tiered(path, out_dir, info)
        ok = "yes" if result["success"] else "NO"
        if result["success"]:
            solved += 1
        tiers = " ".join(
            f"{a['fov_bounds'][0]:.0f}-{a['fov_bounds'][1]:.0f}"
            + ("✓" if a["success"] else "✗")
            for a in result["attempts"]
        )
        print(f"{name[:32]:<32} {tiers:>18} {ok:>7} {result['total_seconds']:>8.1f}")
        if not result["success"]:
            print(f"    log: {result['log_tail'].splitlines()[-1] if result['log_tail'] else '(empty)'}")

    print(f"\n{solved}/{len(photos)} solved")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/photos")
