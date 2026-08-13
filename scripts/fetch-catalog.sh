#!/usr/bin/env bash
# Fetch the HYG star database (bright-star catalog with proper names), the
# JPL DE421 planetary ephemeris (Moon/planet positions for the ephemeris
# layer), and Stellarium's modern-skyculture constellation line set (pinned
# tag so the cache key stays honest).
# https://github.com/astronexus/HYG-Database
set -euo pipefail
cd "$(dirname "$0")/../catalogs"

URL="https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv"
DSO_URL="https://raw.githubusercontent.com/astronexus/HYG-Database/main/misc/dso.csv"
EPH_URL="https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp"
CON_URL="https://raw.githubusercontent.com/Stellarium/stellarium/v23.4/skycultures/modern/constellationship.fab"

if [ -f hyg.csv ]; then
  echo "have hyg.csv"
else
  echo "fetching hyg.csv"
  curl -fL -o hyg.csv "$URL"
fi

if [ -f dso.csv ]; then
  echo "have dso.csv"
else
  echo "fetching dso.csv"
  curl -fL -o dso.csv "$DSO_URL"
fi

if [ -f de421.bsp ]; then
  echo "have de421.bsp"
else
  echo "fetching de421.bsp"
  curl -fL -o de421.bsp "$EPH_URL"
fi

if [ -f constellations.fab ]; then
  echo "have constellations.fab"
else
  echo "fetching constellations.fab"
  curl -fL -o constellations.fab "$CON_URL"
fi
