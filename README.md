# asterism

Annotate a night-sky phone photo: upload a shot, plate-solve it with
astrometry.net, and get back labels for every star (and eventually planet,
satellite, and a bit of narration) in the frame.

Final home: `asterism.quietlife.net`.

## How it works

- `web` — FastAPI app. `POST /jobs` accepts an image upload and queues a job;
  `GET /jobs/{id}` reports status/results; `/` serves a barebones upload page
  that polls and draws labels on a canvas overlay.
- `worker` — same image, different command. Polls the SQLite job table, shells
  out to `solve-field` with scale hints derived from EXIF focal length, parses
  the resulting WCS with astropy, and projects a bright-star catalog (HYG) into
  pixel coordinates.
- Jobs/results live in `data/` (SQLite + uploaded images), bind-mounted into
  both containers.

Phone photos have wide fields of view (~30–90°), which solve against the
*wide* astrometry.net indexes — the small ones. The multi-GB index sets are
only needed for narrow (telescope) fields, which are out of scope for v1.

## Quickstart

```
./scripts/fetch-indexes.sh    # wide-field 4100-series indexes (~100MB)
./scripts/fetch-catalog.sh    # HYG bright-star catalog
docker compose up -d --build
```

Then open http://localhost:8000 and upload a night-sky photo.

## Benchmark

The first real question for this project is the solve success rate on typical
phone shots. Drop some real night-sky photos into `photos/` and run:

```
docker compose run --rm worker python -m app.bench /photos
```

It prints per-image solve success, timing, and the scale hints used.

## Plan

1. **Local MVP** (this repo): upload → solve → star labels on canvas. ← you are here
2. **Ephemeris layer**: Skyfield + EXIF time/GPS → label the Moon and planets
   (the thing astrometry.net can't do). Graceful-failure path: no solve, but
   time + GPS + compass heading → "you were facing SW, that was probably Jupiter".
3. **Fly.io deploy**: single app, web + worker processes, indexes baked into
   the image.
4. **Differentiators**: satellite/streak ID from archived TLEs, LLM narration,
   shareable cards.
