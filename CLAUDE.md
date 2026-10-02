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

## Thresholds are measured, not guessed (#99)
- Any change to `MIN_LOGODDS`, `MIN_MATCHES`, `PRECHECK_MIN_STARS`,
  `FALLBACK_TIERS`, `SOLVE_CPULIMIT` or `SOURCE_DEPTH` reports what it does
  to corpus pass rate — not what it does to the one photo that prompted it.
  `MIN_LOGODDS`/`MIN_MATCHES` were set from six photos and were wrong within
  a day (#86).
- The first three are post-hoc gates: sweep them over a saved run, no
  re-solve needed. `python -m app.bench --sweep run.json --logodds 20,25,30`.
- The last three change what the solver does: save a run on each side and
  `--compare`. Quote the seconds too, not just the pass rate.
- Paste the sweep or compare output in the PR. See README, "Moving a
  threshold". The corpus is an astrophotography dataset, a different
  population from casual phone uploads: it bounds the question, it does not
  settle it.

## The fly-around page (`static/fly/`, `/fly?job=`)
- Its tests are three tiers: `tests/test_fly.py` (the star data and the
  routes, in the fast pytest tier), `tests/js/fit.test.mjs` (the camera
  fit, under node), and `tests/browser/` (the page in Chromium:
  `docker compose --profile browser run --rm --build browser`). The browser
  image bakes its tests in like the worker's: `--build` after editing.
- Judge a change to how it moves by filming it (`node film.js` in the
  browser container, README "From outside"), not by stills: every wrong
  version of the camera's exit from Earth looked fine frame by frame.
- Check it at a phone's size too (`VIEWPORT=390x844`). Most photos and
  visitors come from phones, and an upright screen is where the framing
  breaks first.
- `static/fly/vendor/three/` is checked in. Change the version in
  `scripts/vendor-three.sh` and rerun it; do not hand-edit.

## Deploy workflow
- Deploy BEFORE merging: `fly deploy` from the feature branch, verify it works
  in prod, and only then merge the PR. main stays pristine as the rollback
  target — if the deploy misbehaves, redeploy main.

## Domain gotchas
- HYG catalog: `ra` column is in HOURS (multiply by 15); `proper` is the name
  column; the Sol row must stay excluded.
- solve-field: wide-field solves are 1–10s with correct scale bounds; nearly all
  latency variance is hint quality, not compute. Failures burn the full cpulimit
  per tier.
- Astrophotography sources can exceed 8000px in either dimension — check
  `identify` before reading any image file into the conversation.
