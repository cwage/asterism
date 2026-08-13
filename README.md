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
  pixel coordinates. Failures are gated (#4): a ~1s star-count pre-check
  rejects zero-star uploads instantly, and a quick solve tries only the
  likeliest scale tier — the client can then POST `/jobs/{id}/deepen` to
  opt into the slower fallback tiers. When the photo has an EXIF timestamp, the Moon and
  naked-eye planets are computed with skyfield (JPL DE421, topocentric when
  GPS is present) and projected through the same WCS. Constellation stick
  figures (Stellarium's modern line set, resolved via HYG) are drawn the
  same way. When a solve fails outright, the same ephemeris still answers
  the question from EXIF alone (#7): timestamp + GPS + compass heading
  (declination-corrected via the World Magnetic Model) → "you were facing S;
  the bright object was Venus, WSW, to your right".
- After projection, labels are verified against the pixels: each star label
  is matched to a detected source near its predicted position, a smooth
  residual field fitted from the matches corrects for computational-stack
  warp (phone night modes can drag stars ~1° toward moving clouds — #28),
  and stars with no visible source are flagged `hidden`. Deep-sky objects
  get an extended-source check instead of point snapping (#50): core
  brightness against a surrounding annulus (or resolved member stars for
  clusters), so a label never circles "Andromeda Galaxy" over bare sky-glow.
- Jobs/results live in `data/` (SQLite + uploaded images), bind-mounted into
  both containers.
- The queue is deliberately single-worker: solve-field is CPU-bound and the
  deploy is one shared-CPU machine, so concurrency would just make every
  solve slower. FIFO by (created_at, id); the status API reports
  `queue_position` for queued jobs; orphaned `solving` rows are re-queued at
  worker startup.

Phone photos have wide fields of view (~30–90°), which solve against the
*wide* astrometry.net indexes — the small ones. The multi-GB index sets are
only needed for narrow (telescope) fields, which are out of scope for v1.

## Quickstart

```
./scripts/fetch-indexes.sh    # wide-field 4100-series indexes (~100MB)
./scripts/fetch-catalog.sh    # HYG catalog + DE421 ephemeris (~17MB) + constellation lines
docker compose up -d --build
```

Then open http://localhost:8000 and upload a night-sky photo.

## Tests

Fast tier (pure logic — EXIF/FOV math, tier selection, catalog projection):

```
docker compose run --rm worker pytest
```

Slow tier (real `solve-field` runs against synthetic star fields rendered
from the HYG catalog with a known WCS, plus obviously-unsolvable images):

```
docker compose run --rm worker pytest -m solver
```

The synthetic fields give exact ground truth: the test asserts the solved
pointing lands within 1.5° of where the field was rendered, not just that
the solver said yes.

## Benchmark

The first real question for this project is the solve success rate on typical
phone shots. Drop some real night-sky photos into `photos/` and run:

```
docker compose run --rm worker python -m app.bench /photos
```

It prints per-image solve success, timing, and the scale hints used.

## Plan

1. **Local MVP** (this repo): upload → solve → star labels on canvas. ✅
2. **Ephemeris layer**: Skyfield + EXIF time/GPS → label the Moon and planets
   (the thing astrometry.net can't do). ← you are here. Still open: proper
   timezone handling (#6).
3. **Fly.io deploy**: single app, web + worker processes, indexes baked into
   the image.
4. **Differentiators**: satellite/streak ID from archived TLEs, LLM narration,
   shareable cards.
