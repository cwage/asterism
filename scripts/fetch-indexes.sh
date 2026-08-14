#!/usr/bin/env bash
# Fetch the wide-field astrometry.net index files (4100 series, Tycho-2).
# Scales 4110-4119 cover quads from ~1 degree up to ~30+ degrees, which is
# plenty for phone-camera fields of view (~30-90 degrees wide). Grab down to
# 4108 to leave margin for zoomed shots.
#
# In practice 4108 buys more than "margin": measured 2026-08-14, this set
# solves synthetic fields down to ~2.5 degrees, which covers every phone
# telephoto (a 10x periscope is ~8.6 deg). test_solve_integration.py pins
# that down — narrow FIRST here without re-running it and the telephoto
# tier in solver.py silently stops working. Going below ~2.5 deg means the
# 4200 series, which is healpix-tiled and runs to gigabytes (#19).
set -euo pipefail
cd "$(dirname "$0")/../indexes"

FIRST="${1:-4108}"
LAST="${2:-4119}"

for i in $(seq "$FIRST" "$LAST"); do
  f="index-${i}.fits"
  if [ -f "$f" ]; then
    echo "have $f"
  else
    echo "fetching $f"
    curl -fL -o "$f" "https://data.astrometry.net/4100/${f}"
  fi
done
