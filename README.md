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
  the question from EXIF alone (#7): timestamp + GPS → "the bright object was
  Venus, WSW, 12° up". Directions are absolute compass bearings, not relative
  to how the phone was aimed: the EXIF heading turned out to be wrong by 60 to
  160° on real frames whose true pointing a solve could confirm, so nothing
  user-facing is built on it (#81). Without GPS the location
  comes from the clock's UTC offset, which is ambiguous by a whole zone —
  the offset fits both a standard-time and a daylight-saving meridian (#79)
  — so both are checked, a body counts as visible if it clears the horizon
  under either, and the altitude is quoted as the range rather than a
  precision the data doesn't have.
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
  The results page puts the caption and narration on the clipboard in one
  tap for a post or alt text, and the label list in another (#124).
- Bright objects just outside the frame (#118): stars to magnitude 2, the
  Moon and planets, and the showpiece deep-sky objects are projected through
  the same WCS and, when one lands within 15° of an edge, drawn as a coral
  arrow at the edge with the name and how far off it lies — on the page and
  the card, at most six, never displacing a label for something in the shot.
  Directions are the photo's own (left, right, above, below). The narration
  gets them as facts, so it stops guessing what lies off the edge.
- What the night was like (#121) and how deep the photo reached (#122):
  a sentence or two of measured context under the narration. The Sun's
  altitude at the EXIF instant says whether the shot was in twilight and
  about how long before full dark; the Moon's phase, and whether it was
  up, says what lit the sky. Without GPS these are judged across every
  plausible observer position in the clock offset's timezone band, minus
  positions from which the solved field was below the horizon or the Sun
  above it, and stated only when they all agree. The depth line comes
  from the verification pass: the catalog to magnitude 9 is projected
  through the WCS and tested bin by bin for a detected source, and the
  limiting magnitude is where the detection rate falls through half of
  the bright end's, with a star-free control beside every test so a noisy
  frame reports nothing rather than something flattering, and no answer
  at all when even the bright stars are mostly hidden. The sentences are built in the worker
  so the page, the copied description and the narration (which gets them
  as facts) all say the same thing.
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
Nor does a hidden job come back when the same file is uploaded again (#120):
the re-upload becomes a fresh job, which you hide the same way.

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

### Kept by the uploader

Featuring is operator-only, so the solves that survived the sweep used to be
the ones the operator happened to notice in time; a set of six good frames
from one traveler came in overnight and the best were gone before anyone
looked. The person with the strongest claim on whether a photo stays up is
the one who took it, so the result page offers "keep this solve on the site"
(#113) while the window is open. It sets a separate `kept` flag through
`POST /jobs/{id}/keep`, with no token: the job id is the only access control
there is (#21), and it is already the link. "kept on the site — undo" calls
`/unkeep`, after which the next sweep collects the job if it is past the
window. Only a solved, un-hidden job inside the window can be kept, and
hiding clears the flag like it clears `featured`, so nothing is both
invisible and immortal. `KEEPS_PER_DAY` (6) caps keeps per client address
in a sliding day, so one person can't pin the whole feed; `/unfeature` does
not touch `kept` and `/unkeep` does not touch `featured`, so an operator's
showcase and an uploader's keep can each be undone without the other.
The nightly digest counts kept solves separately, since they need no
action.

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

## Following new solves

"Your recent uploads" (#105) bookmarks this browser's last 24 submissions in
`localStorage`: job IDs, filenames, and submission times only. Each submission
has its own storage key rather than a shared read/modify/write array,
so uploads and expiry in different tabs cannot overwrite unrelated bookmarks.
The list is not tied to the daily uploader hash, and opening someone else's result doesn't add it
to the list. Status and captions are fetched on page load, when the window
regains focus, or with "Refresh status"; ongoing uploads also update their
entry as they are polled. Failed and deeper solves remain easy to return to.
Each refresh has a single ten-second deadline, including response bodies, so
an unreachable API cannot lock the refresh button for minutes.
Only a confirmed 404/410 removes an unavailable result, not a network error
or a guessed expiry time, so featured results can outlive the usual window.
This does not change server retention. Clearing site data clears the list;
other browsers have separate lists, and uploads made before this feature
aren't recovered. If storage is blocked, the list lasts only in the open tab.

The homepage thumbnails carry up to two small sky labels (#126), such as
"Milky Way core", "Summer Triangle", or "Pleiades". A deterministic table
uses the solved constellations and non-hidden, in-frame object labels;
otherwise it names the constellation of the brightest non-hidden catalog
star. Region tags describe where the camera pointed, not whether Milky Way
light was detected. They are derived on read, so older retained solves gain
them too, without a new solve or model call.

`/feed.atom` is the homepage strip as an Atom feed (#127): the same 24 newest
successful solves, caption as the entry title, the share card inline and as an
enclosure, and a link to the result page. It is the one way to follow the site
that asks nothing of the reader — there is no account system to hang
notifications on — and the homepage advertises it with a `<link
rel="alternate">`, so a feed reader finds it from the site URL alone.

Entries expire with retention like everything else. A reader keeps what it
fetched, but the card and result links behind an expired entry 404 like any
other expired link; a featured solve simply stays valid.

## Activity notifications

Nothing else reports what the site did today: counts exist only as rows the
retention sweep deletes, and a burst of uploads is visible only to whoever
happens to look at the homepage. Set `NTFY_TOPIC_URL` to an
[ntfy.sh](https://ntfy.sh) topic and the worker publishes two things (#69):

- a roughly-nightly summary —
  `47 uploads · 39 solved · 8 failed (6 no_stars, 2 no_match) · 2 hidden · 10 featured`
- a burst alert when solves outpace what this site normally sees —
  `9 solves in the last 60 minutes`

```
fly secrets set NTFY_TOPIC_URL=https://ntfy.sh/some-unguessable-name
```

Treat that URL as a credential: anyone holding it can read the notifications
*and* publish to them, so use a name nobody will guess, or ntfy's own auth. It
is never logged, including on failure. Unset means the feature is off, the same
as `ANTHROPIC_API_KEY` and `SPACETRACK_*`.

| variable | default | |
| --- | --- | --- |
| `NTFY_TOPIC_URL` | *(unset — off)* | where to publish |
| `NTFY_BURST_SOLVES` | `6` | solves in the window that trip the alert |
| `NTFY_BURST_WINDOW_MINUTES` | `60` | how long that window is |
| `NTFY_SUMMARY_HOUR_UTC` | `7` | earliest hour the daily summary goes out |
| `NTFY_TICK_SECONDS` | `600` | how often the worker evaluates both |

The tick lives in the worker loop rather than a scheduled job, which means it
only runs while the machine is up. That failure mode is exactly correlated with
having nothing to report — `auto_stop_machines` only stops the machine when
nobody has uploaded — so the summary drifts to the first tick after the next
wake, on a day when it reads `0 uploads` anyway. The burst alert is unaffected:
it fires while the traffic is happening, which is the whole point of it. A
wake gap longer than `RETENTION_HOURS` undercounts the summary, since it reads
live rows and the swept ones are gone; the daily history below keeps the
long-run counts.

The purpose is volume awareness, not abuse forensics. The summary says how
many distinct uploaders it saw (`12 uploads from 5 people`), which is enough to
tell fifteen real people from one person to reach for the kill switch about;
who any of them are is not recorded anywhere (see below).

## Who uploaded what, without keeping who

Uploads are anonymous and the client address is never stored (#116). What a
job row keeps instead:

- `uploader_hash` — an HMAC of the client address under a salt that is minted
  on first use each UTC day (`meta` key `salt:YYYY-MM-DD`) and deleted by the
  retention sweep once the day is over. Uploads from one address on one day
  share a token. Once the day's salt is gone nothing, the database included,
  can turn a token back into an address or join it to another day's; while
  the day is live the salt is in the database beside the tokens, so a copy
  taken that day could test candidate addresses against them. The promise is
  that yesterday is unrecoverable, not that today is.
- `device_json` — camera make, model and software from the file's own EXIF.
  The served file carries these already (#117 is about that); they are kept
  out of `exif_json`, which `/jobs/{id}` serves, so they never reach a public
  payload.

The retention sweep folds every expiring row into `daily_stats` (uploads,
solved, failed by reason, hidden, per UTC day of upload) and the day's distinct
tokens into `daily_uploaders` before it deletes anything, in the same
transaction, so a row is either counted and gone or neither. Featured rows are
never deleted and are counted once (`jobs.counted`). Read it back with:

```
fly ssh console -C "python3 -c 'from app import db, stats; import json; conn = db.get_conn(); print(json.dumps(stats.history(conn, 30), indent=1))'"
```

Within the retention window the live rows answer the sharper question — which
of the last day's uploads came from the same address:

```
SELECT uploader_hash, COUNT(*) FROM jobs WHERE created_at > datetime('now', '-24 hours') GROUP BY 1
```

The cutoff matters: featured rows outlive the window, and without it they
would mix older days into the answer. The salt rotates at UTC midnight, so a
person active on both sides of it counts twice in any window that spans it; a
small overcount, in the honest direction.

## The same photo twice

An upload is hashed (SHA-256) as received, before the orientation bake
re-encodes it, and the hash stays on the row (`jobs.content_hash`, indexed). A
second send of the same bytes gets the first job back (#120): `POST /jobs`
answers with the existing id, its status, and `"duplicate": true`, and the page
lands on that result in whatever state it is in, with a note on the status line
saying why it was instant. Two of one week's fourteen uploads were exact
repeats, and each one used to spend a bake and a solve on an answer that
already existed. Hidden rows never match, so a takedown cannot be undone by
sending the file again. A messaging-app copy has different bytes and is a
different photo here, on purpose: near-duplicate detection is its own feature,
and the WCS-overlap link (#119) covers the interesting half of it.

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

The original roadmap, all four steps of it now shipped:

1. **Local MVP** (this repo): upload → solve → star labels on canvas. ✅
2. **Ephemeris layer**: Skyfield + EXIF time/GPS → label the Moon and planets
   (the thing astrometry.net can't do). ✅ Timezone handling (#6) landed with
   it: EXIF offset when present, else the IANA zone at the GPS fix, else a
   longitude guess, else UTC — and the results report which one was used.
3. **Fly.io deploy**: single app, web + worker processes, indexes baked into
   the image. ✅
4. **Differentiators**: satellite/streak ID from archived TLEs, LLM narration,
   shareable cards. ✅ Streak *detection* stayed out of scope — satellite
   tracks are computed from TLEs and drawn dashed to say so.
