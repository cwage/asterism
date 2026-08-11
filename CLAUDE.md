# asterism — project notes for agents

## Everything runs in docker compose
- Don't assume host tooling (Python, astrometry.net, ImageMagick) — development
  roams across workstations with different setups. The containers are the only
  supported environment. Fast tests: `docker compose run --rm worker pytest`.
  Solver tests: add `-m solver`. Bench: `docker compose run --rm -T worker
  python -u -m app.bench <dir>`.
- Tests and app code are baked into the image at build time (Dockerfile `COPY`)
  — run `docker compose build worker` after editing, before running them in
  the container.
- Some hosts run rootless docker: never add a `user:` override to compose
  services (see comment in docker-compose.yml; it breaks bind-mount writes
  under rootless).
- Long container runs: use `python -u` (or `PYTHONUNBUFFERED=1`) when capturing
  output — block buffering once ate 45 minutes of bench results.

## Data files (all gitignored, never commit)
- `scripts/fetch-indexes.sh` / `fetch-catalog.sh` populate `indexes/` and
  `catalogs/`. CI caches both, keyed on the scripts' hashes — edit URLs/ranges
  only in the scripts so the cache invalidates correctly.
- Bench corpus: `photos/astro-high-res/` — 197 images from AstroSmartphoneDataset
  (Zenodo 10.5281/zenodo.14933725). License is CC BY-NC-ND: never commit or
  redistribute these images or derivatives. This is why test fixtures are
  synthetic (`tests/synth.py`) — keep it that way.
- Full-corpus bench takes hours (~45s/image, ~200s per failure). Sample instead.

## Domain gotchas
- HYG catalog: `ra` column is in HOURS (multiply by 15); `proper` is the name
  column; the Sol row must stay excluded.
- solve-field: wide-field solves are 1–10s with correct scale bounds; nearly all
  latency variance is hint quality, not compute. Failures burn the full cpulimit
  per tier.
- Astrophotography sources can exceed 8000px in either dimension — check
  `identify` before reading any image file into the conversation.
