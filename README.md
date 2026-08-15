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
  opt into the slower fallback tiers. A solve is only accepted if its match
  clears a confidence floor (#71): solve-field exits 0 and writes a WCS even
  for matches built from a handful of stars, which point somewhere confidently
  wrong, so `SOLVE_MIN_LOGODDS`/`SOLVE_MIN_MATCHES` are checked against
  `solve.match` before anything is projected. Failures distinguish a finished
  search that found nothing (`no_match`) from one the CPU limit cut short
  (`timeout`, or `partial_timeout` when only some tiers ran out) — the second
  rules nothing out, and saying "no solution" for it is a lie (#72).
  When the photo has an EXIF timestamp, the Moon and
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
- Satellite crossings (#11): when the photo carries both a timestamp and
  GPS, Space-Track element sets (`SPACETRACK_USER`/`SPACETRACK_PASS`) are
  propagated with sgp4 across the EXIF exposure window and projected
  through the same WCS, listing what passed through the frame while the
  shutter was open. Tracks are drawn dashed because they are computed,
  not detected — streak detection in the pixels is deliberately out of
  scope. TLE sets are cached per UTC date under `data/tle/`, so a night of
  uploads costs one query.
- With an `ANTHROPIC_API_KEY` configured (Fly secret in prod), each solved
  photo also gets a short LLM-written "what you captured" narration (#12,
  Claude Haiku over the label list — never the photo): a writeup on the
  results page and a one-line caption that replaces the deterministic one
  on the share card. Best-effort: no key or a failed call just skips it.
- Jobs/results live in `data/` (SQLite + uploaded images), bind-mounted into
  both containers.
- The queue is deliberately single-worker: solve-field is CPU-bound and the
  deploy is one shared-CPU machine, so concurrency would just make every
  solve slower. FIFO by (created_at, id); the status API reports
  `queue_position` for queued jobs; orphaned `solving` rows are re-queued at
  worker startup.

Phone photos have wide fields of view (~30–90°), which solve against the
*wide* astrometry.net indexes — the small ones. Phone **telephoto** shots
work too, without any extra data: the shipped set reaches down to ~2.5°,
and a 10x periscope is only ~8.6° wide. The multi-GB index sets are needed
below that, for genuine telescope fields, which stay out of scope (#19).

## Moderation

Uploads are anonymous, solving is not a content filter, and every successful
solve is republished on the homepage feed. So there is a kill switch (#60):
with `ADMIN_TOKEN` set (a Fly secret in prod), one request pulls a job out of
every public read path.

### Runbook: taking a photo down

**1. Get the job id.** Tap the thumbnail on the homepage — the URL becomes
`https://asterism.quietlife.net/?job=<32 hex chars>`. To avoid opening a photo
you are trying to get rid of, list the feed instead (newest first, same order
as the strip; the captions usually identify it):

```
curl -s https://asterism.quietlife.net/feed | jq -r '.jobs[] | "\(.id)  \(.caption // "-")"'
```

**2. Hide it.**

```
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://asterism.quietlife.net/jobs/JOB_ID/hide
```

Expect `{"id":"...","hidden":true}`.

**3. Confirm.** `404` means gone, and the homepage strip drops it on reload:

```
curl -s -o /dev/null -w "%{http_code}\n" https://asterism.quietlife.net/jobs/JOB_ID
```

Keep `ADMIN_TOKEN` somewhere you can reach from a phone. It is a Fly secret,
which is write-only — `fly secrets list` shows digests, never values — so if
the only copy is lost the fix is to set a new one, not to recover it.

### If you hide the wrong one

```
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://asterism.quietlife.net/jobs/JOB_ID/unhide
```

Only works inside the retention window — once the sweep has deleted the row and
the bytes, nothing brings it back. Note that hiding clears `featured`, and
unhiding does not restore it: re-featuring is a separate decision.

### What hiding does, and what it doesn't

The job 404s from `/feed`, `GET /jobs/{id}`, `/image`, and `/card` with the same
copy an expired job gets — a hidden job is indistinguishable from one that never
existed, so an abuser learns nothing from the response. The cached card PNG is
unlinked immediately: `?job=` points OpenGraph at the card, so already-posted
share links stop unfurling the image at the same moment. A job that hasn't
solved yet also stops being claimable, so it can't burn a solve on its way out.

The row and the upload stay on disk until the retention sweep collects them,
which is what makes a mistyped id recoverable and keeps the bytes available if
an upload needs reporting rather than just removing.

What it does **not** do is stop the person. There is no ban and no IP block, and
the per-IP cap is `UPLOADS_PER_HOUR` (12), so someone actively poking can
re-upload faster than you can hide. Against a sustained attack the levers are
blunt and hit everyone. `fly secrets set UPLOADS_PER_HOUR=0 -a asterism` stops
new uploads while leaving existing results readable;
`fly scale count 0 -a asterism` takes the site down.

That gap is accepted on purpose, not pending. Automatic filtering (#61),
per-uploader feed caps (#62), and an opt-in feed (#63) were all considered and
closed: with effectively one uploader and little traffic, a fast takedown is
proportionate, and the alternatives cost more than they'd save. Worth knowing
before reaching for the obvious fix — #61 has the measurements showing that a
brightness-based "does this look like a night sky" filter rejects real
light-polluted skies, so it fails as a filter rather than merely needing
tuning. Those issues carry the reasoning and their implementation sketches; the
assumption holding them closed is that the uploader is the operator, so reopen
them if that changes.

With no `ADMIN_TOKEN` configured the endpoint 404s for everyone — unset means
absent, not open, so local dev and CI have nothing to poke at.

## Featuring a solve

Everything is deleted after `RETENTION_HOURS`, which leaves the homepage feed
empty whenever nobody has uploaded in a day — bad for a site that has to explain
itself to someone arriving cold. Featuring a job (#67) exempts it from the
retention sweep, so a handful of good solves stay on as permanent examples:

```
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://asterism.quietlife.net/jobs/JOB_ID/feature
```

`/unfeature` puts it back in the normal retention window, where the next sweep
collects it if it is already older than `RETENTION_HOURS` — usually the point.

Same `ADMIN_TOKEN` gate as the kill switch, and the same way of finding a job id
(see the runbook above). Only a solved job can be featured, and a hidden one
can't be: featuring is a request that the sweep never collect something, which
is the wrong thing to ask about a job that has been pulled from the site.
Hiding therefore clears the flag — the kill switch always wins, so nothing ends
up invisible *and* immortal.

Featuring changes retention, not placement. The feed is still
`ORDER BY created_at DESC LIMIT 24`, so a featured solve is kept forever but
sinks out of the strip once 24 newer solves exist. On a quiet site that never
happens, which is the case this exists for. Storage grows monotonically by
design; a few dozen phone JPEGs and their cards is nothing against the volume.

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
