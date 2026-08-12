FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends astrometry.net netpbm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini conftest.py ./

ENV ASTROMETRY_INDEX_DIR=/indexes \
    CATALOG_DIR=/catalogs \
    DATA_DIR=/data

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Local dev / CI target: data dirs come from bind mounts (docker-compose)
# or the CI actions/cache — the image stays lean.
FROM base AS app

# Sky-data fetch stage for the release image. Reuses the exact scripts CI
# hashes for its cache keys, so the two stay in sync (#9). BuildKit skips
# this stage entirely when building the `app` target.
FROM python:3.12-slim AS skydata
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /fetch
RUN mkdir indexes catalogs
COPY scripts/fetch-indexes.sh scripts/fetch-catalog.sh scripts/
RUN scripts/fetch-indexes.sh && scripts/fetch-catalog.sh

# Release target (Fly deploy): indexes + catalog + ephemeris baked in so
# machines stay stateless; only /data (SQLite + uploads) needs a volume.
FROM base AS release
COPY --from=skydata /fetch/indexes /indexes
COPY --from=skydata /fetch/catalogs /catalogs
