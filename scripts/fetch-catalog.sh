#!/usr/bin/env bash
# Fetch the HYG star database (bright-star catalog with proper names) and the
# JPL DE421 planetary ephemeris (Moon/planet positions for the ephemeris layer).
# https://github.com/astronexus/HYG-Database
set -euo pipefail
cd "$(dirname "$0")/../catalogs"

URL="https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv"
EPH_URL="https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp"

if [ -f hyg.csv ]; then
  echo "have hyg.csv"
else
  echo "fetching hyg.csv"
  curl -fL -o hyg.csv "$URL"
fi

if [ -f de421.bsp ]; then
  echo "have de421.bsp"
else
  echo "fetching de421.bsp"
  curl -fL -o de421.bsp "$EPH_URL"
fi
