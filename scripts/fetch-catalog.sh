#!/usr/bin/env bash
# Fetch the HYG star database (bright-star catalog with proper names).
# https://github.com/astronexus/HYG-Database
set -euo pipefail
cd "$(dirname "$0")/../catalogs"

URL="https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv"

if [ -f hyg.csv ]; then
  echo "have hyg.csv"
else
  echo "fetching hyg.csv"
  curl -fL -o hyg.csv "$URL"
fi
