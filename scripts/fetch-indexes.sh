#!/usr/bin/env bash
# Fetch the wide-field astrometry.net index files (4100 series, Tycho-2).
# Scales 4110-4119 cover quads from ~1 degree up to ~30+ degrees, which is
# plenty for phone-camera fields of view (~30-90 degrees wide). Grab down to
# 4108 to leave margin for zoomed shots.
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
