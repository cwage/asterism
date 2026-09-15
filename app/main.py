import asyncio
import hashlib
import hmac
import html
import json
import math
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.routing import APIRoute

from . import card, db, exif, sky_tags, stats

app = FastAPI(title="asterism")
db.init_db()

UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Abuse limits (#10): every accepted upload is worker CPU (worst case ~200s
# for an unsolvable image in deep mode), so the open endpoint gets caps.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
# Decoded size is capped separately from the byte count: the orientation
# bake in create_job decodes the whole frame in the web process, and the
# byte cap only bounds compressed data. 100MP clears every phone and
# full-frame body; a 20MB JPEG that opens to more is 0.2 bytes a pixel — a
# flat frame or a decompression bomb, never a sky.
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", str(100 * 1000 * 1000)))
UPLOADS_PER_HOUR = int(os.environ.get("UPLOADS_PER_HOUR", "12"))
# Keeps (#113) are open to anyone holding a result link, so one person
# must not be able to pin fifty frames: a per-address daily ceiling, in
# the upload limiter's shape.
KEEPS_PER_DAY = int(os.environ.get("KEEPS_PER_DAY", "6"))
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "20"))

# Expired links 404 identically to typos; say why that might be (#23).
RETENTION_HOURS = int(os.environ.get("RETENTION_HOURS", "24"))
_GONE = f"no such job (results expire after {RETENTION_HOURS} hours)"

# Moderation kill switch (#60). Uploads are anonymous and successful solves
# are republished on the homepage, so there has to be a way to pull one down
# that isn't "ssh in and edit SQLite by hand". Unset means the endpoint does
# not exist at all — local dev and CI have nothing to poke.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

_upload_log = defaultdict(deque)  # client ip -> recent upload monotonic times
_keep_log = defaultdict(deque)    # client ip -> recent keep monotonic times

# The orientation bake holds a decoded frame (and its transposed copy) in
# memory and runs in the threadpool that also serves the sync handlers. One
# at a time: the queue-depth check runs before it, so a burst that passed
# the gate would otherwise stack that many decodes at once. A phone frame
# takes ~0.1s, so the wait is invisible.
_orient_slot = asyncio.Semaphore(1)


def _discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _require_admin(request):
    """404 rather than 401/403: an unauthenticated caller learns nothing about
    whether the endpoint (or the job) is there."""
    if not ADMIN_TOKEN:
        raise HTTPException(404, "Not Found")
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str, and
    # headers arrive latin-1-decoded, so a junk header would 500 the endpoint.
    sent = request.headers.get("authorization", "").encode("utf-8", "replace")
    if not hmac.compare_digest(sent, f"Bearer {ADMIN_TOKEN}".encode()):
        raise HTTPException(404, "Not Found")


def _client_ip(request):
    # Fly's proxy puts the real client address in Fly-Client-IP; the socket
    # peer is the proxy itself. Fall back for local dev.
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else "unknown"))


def _window_limited(logs, ip, limit, window, now=None):
    """Sliding window per client IP. Counts attempts, not successes, so a
    rejected request isn't a free retry."""
    now = time.monotonic() if now is None else now
    log = logs[ip]
    while log and log[0] <= now - window:
        log.popleft()
    if len(log) >= limit:
        return True
    log.append(now)
    if len(logs) > 10000:  # shed empty entries under IP churn
        for key in [k for k, v in logs.items() if not v][:5000]:
            del logs[key]
    return False


def _rate_limited(ip, now=None):
    """Uploads: UPLOADS_PER_HOUR in a sliding hour."""
    return _window_limited(_upload_log, ip, UPLOADS_PER_HOUR, 3600, now)


def _keep_limited(ip, now=None):
    """Keeps (#113): KEEPS_PER_DAY in a sliding day."""
    return _window_limited(_keep_log, ip, KEEPS_PER_DAY, 86400, now)


def _queue_depth():
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE status IN ('queued', 'solving') AND hidden = 0"
        ).fetchone()
    return row["n"]


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _same_upload(conn, content_hash):
    """The job already made from these exact bytes, as an upload response,
    or None (#120). Whatever its state: a solve still running is joined, a
    finished one is shown, a failed one fails again the same way, and the
    deepen button is still there. Hidden rows never match — a takedown
    must not be undone by sending the file again — so the re-upload gets a
    fresh job, which hides the same way."""
    row = conn.execute(
        "SELECT id, status FROM jobs WHERE content_hash = ? AND hidden = 0 "
        "ORDER BY created_at DESC LIMIT 1", (content_hash,),
    ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "status": row["status"], "duplicate": True}


@app.get("/")
def index(request: Request, job: str | None = None):
    # Share links (?job=...) get OpenGraph tags pointing at the rendered
    # card (#13) so unfurls show the annotated photo. Job ids are uuid4
    # hex; anything else is served untouched (the frontend handles bad ids).
    if job and re.fullmatch(r"[0-9a-f]{32}", job):
        with open("static/index.html") as f:
            page = f.read()
        base = str(request.base_url).rstrip("/")
        meta = (
            '<meta property="og:title" content="asterism — what you saw">\n'
            '<meta property="og:description" content="A night-sky photo, '
            'plate-solved and labeled from its star pattern.">\n'
            f'<meta property="og:image" content="{base}/jobs/{job}/card">\n'
            '<meta name="twitter:card" content="summary_large_image">\n'
        )
        return HTMLResponse(page.replace("</head>", meta + "</head>"))
    return FileResponse("static/index.html")


@app.post("/jobs")
async def create_job(request: Request, image: UploadFile):
    if _rate_limited(_client_ip(request)):
        raise HTTPException(429, "rate limit: try again in a bit")

    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"image too large (max {MAX_UPLOAD_BYTES // (1024*1024)}MB)")

    # Same bytes, same answer (#120). Hashed as received, before the bake
    # re-encodes the file: a phone's second send of one frame matches its
    # first, and a messaging app's re-compressed copy does not (that is a
    # different photo to every reader downstream, and a different feature).
    # Looked up before the queue gate and before anything touches the
    # disk: a re-upload enqueues, bakes and solves nothing, so a full
    # queue is no reason to turn it away, and the common case, the same
    # file again minutes later, costs neither a write nor a bake. The
    # body read it costs a rejected upload is bounded by the rate limit.
    content_hash = await run_in_threadpool(_sha256, data)
    with db.get_conn() as conn:
        existing = _same_upload(conn, content_hash)
    if existing:
        await image.close()
        return existing

    if _queue_depth() >= MAX_QUEUE_DEPTH:
        await image.close()
        raise HTTPException(503, "solve queue is full: try again in a few minutes")

    # Full 128 bits: the result URL is the only access control (#21).
    job_id = uuid.uuid4().hex
    ext = os.path.splitext(image.filename or "")[1].lower() or ".jpg"
    image_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")
    with open(image_path, "wb") as f:
        f.write(data)
    # The body is on disk. Let go of the in-memory copy and the spooled
    # upload behind it before parking on the bake slot below: a burst that
    # passed the gates above would otherwise hold that many 20MB buffers
    # while waiting its turn, with no job row yet for the depth gate to see.
    del data
    await image.close()

    try:
        width, height = exif.dimensions(image_path)      # header only
    except Exception as e:
        os.unlink(image_path)
        raise HTTPException(400, f"could not read image: {e}")
    if width * height > MAX_IMAGE_PIXELS:
        os.unlink(image_path)
        raise HTTPException(
            413, f"image too large ({width}x{height}; "
                 f"max {MAX_IMAGE_PIXELS // 1_000_000} megapixels)")

    try:
        # Lay the pixels out the way a viewer shows them before anything
        # reads the file — the dimensions and FOV hint captured just below,
        # the solver, the card, the browser (see exif.normalize_orientation).
        # A 12MP re-encode is a CPU-bound moment; keep it off the event loop.
        async with _orient_slot:
            await run_in_threadpool(exif.normalize_orientation, image_path)
        exif_info = exif.read_exif(image_path)
    except asyncio.CancelledError:
        # Cancelled while waiting on the bake (a server shutdown): the row
        # was never going to be inserted, so drop the file now rather than
        # leave it to the orphan sweep. A bake already mid-encode cannot be
        # interrupted and its replace brings the file back; the sweep
        # collects that one.
        _discard(image_path)
        raise
    except Exception as e:
        _discard(image_path)
        raise HTTPException(400, f"could not read image: {e}")

    # Precise GPS is captured into the job record above (the ephemeris layer
    # wants it); the stored file is served publicly, so scrub it (#22).
    # Fail closed on the *file*, not on the attempt: a strip that returns
    # quietly without removing anything would otherwise serve coordinates,
    # and a strip that raises after a fallback succeeded would reject a
    # perfectly good upload.
    try:
        exif.strip_gps(image_path)
    except Exception:
        pass
    if exif.has_location(image_path):
        os.unlink(image_path)
        raise HTTPException(
            415, "this image carries GPS metadata we can't remove; "
                 "strip location data and re-upload"
        )

    # Uploader record (#116): the camera facts the served file carries
    # anyway, kept out of exif_json because /jobs/{id} serves that; and a
    # hash of the client address under today's salt — never the address.
    # Both best-effort: a job is not worth rejecting over its bookkeeping.
    try:
        device = exif.read_device(image_path)
    except Exception:
        device = None
    with db.get_conn() as conn:
        # Look for the same bytes once more, holding the write lock this
        # time: two sends of one frame seconds apart both pass the check
        # at the top (neither has a row until its bake is done) and would
        # both insert. Under the lock the second sees the first's row and
        # joins it, and only its bake was spent.
        conn.execute("BEGIN IMMEDIATE")
        existing = _same_upload(conn, content_hash)
        if existing is None:
            # One clock read for both the salt day and created_at: an
            # upload straddling UTC midnight must not be hashed under one
            # day and filed under the next, or its token would match
            # nothing.
            now = conn.execute("SELECT datetime('now')").fetchone()[0]
            try:
                uploader = stats.uploader_hash(conn, _client_ip(request), now)
            except Exception:
                uploader = None
            conn.execute(
                "INSERT INTO jobs (id, image_path, exif_json, created_at, "
                "uploader_hash, device_json, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, image_path, json.dumps(exif_info), now, uploader,
                 json.dumps(device) if device else None, content_hash),
            )
    if existing:
        _discard(image_path)
        return existing
    return {"id": job_id, "status": "queued"}


@app.post("/jobs/{job_id}/deepen")
def deepen_job(job_id: str):
    """Re-queue a failed quick job to run the remaining solve tiers."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, mode, hidden FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row or row["hidden"]:
            raise HTTPException(404, "no such job")
        if row["status"] != "failed" or row["mode"] == "deep":
            raise HTTPException(409, "job is not eligible for a deeper solve")
        # result_json is kept: the worker skips tiers the quick pass tried.
        conn.execute(
            "UPDATE jobs SET status = 'queued', mode = 'deep', error = NULL "
            "WHERE id = ?", (job_id,),
        )
    return {"id": job_id, "status": "queued", "mode": "deep"}


@app.post("/jobs/{job_id}/hide")
def hide_job(job_id: str, request: Request):
    """Pull a job out of every public read path (#60).

    The row and the upload stay on disk for the retention sweep to collect:
    hiding is instant, reversible with one UPDATE if the wrong id gets typed,
    and keeps the bytes around in case the upload needs reporting rather than
    just removing."""
    _require_admin(request)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT image_path FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, _GONE)
        # featured = 0 as well: the kill switch outranks the showcase (#67),
        # and kept = 0 for the same reason (#113). Leaving either set would
        # strand a job that is invisible *and* exempt from the sweep, so its
        # bytes would never leave the disk.
        if not conn.execute(
            "UPDATE jobs SET hidden = 1, featured = 0, kept = 0 WHERE id = ?",
            (job_id,)
        ).rowcount:
            raise HTTPException(404, _GONE)  # swept between the two statements
    # The cached card is the amplification path — share links unfurl it (#13)
    # — so drop it now instead of waiting on the sweep.
    if row["image_path"]:
        try:
            os.unlink(row["image_path"] + ".card.png")
        except FileNotFoundError:
            pass
    return {"id": job_id, "hidden": True}


@app.post("/jobs/{job_id}/unhide")
def unhide_job(job_id: str, request: Request):
    """Undo a hide (#67). Previously this meant `fly ssh console` and a
    Python one-liner against the volume, which is a bad thing to be
    improvising when the reason you're doing it is that you hid the wrong id."""
    _require_admin(request)
    with db.get_conn() as conn:
        if not conn.execute(
            "UPDATE jobs SET hidden = 0 WHERE id = ?", (job_id,)
        ).rowcount:
            raise HTTPException(404, _GONE)
    return {"id": job_id, "hidden": False}


@app.post("/jobs/{job_id}/feature")
def feature_job(job_id: str, request: Request):
    """Exempt a job from the retention sweep (#67), keeping it as a permanent
    example so the feed has something in it on a quiet day.

    Refuses hidden jobs: featuring one would be asking the sweep to never
    collect something we have already decided shouldn't be visible."""
    _require_admin(request)
    with db.get_conn() as conn:
        # Every precondition rides in the UPDATE rather than a SELECT before
        # it. Check-then-set loses to anything that commits in the gap: a
        # concurrent /hide would leave the job hidden *and* featured, which is
        # invisible *and* exempt from the sweep, so its bytes would never
        # leave the disk — the one state these two flags must never reach.
        # Work out which error to report only after losing.
        if not conn.execute(
            "UPDATE jobs SET featured = 1 "
            "WHERE id = ? AND hidden = 0 AND status = 'done'",
            (job_id,),
        ).rowcount:
            row = conn.execute(
                "SELECT status, hidden FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:  # swept, or never existed
                raise HTTPException(404, _GONE)
            if row["hidden"]:
                raise HTTPException(409, "unhide the job before featuring it")
            raise HTTPException(409, "only a solved job can be featured")
    return {"id": job_id, "featured": True}


@app.post("/jobs/{job_id}/unfeature")
def unfeature_job(job_id: str, request: Request):
    """Drop a job back into the normal retention window (#67). The next sweep
    collects it if it is already older than RETENTION_HOURS, which is usually
    the point."""
    _require_admin(request)
    with db.get_conn() as conn:
        if not conn.execute(
            "UPDATE jobs SET featured = 0 WHERE id = ?", (job_id,)
        ).rowcount:
            raise HTTPException(404, _GONE)
    return {"id": job_id, "featured": False}


@app.post("/jobs/{job_id}/keep")
def keep_job(job_id: str, request: Request):
    """Let the uploader keep a solve past the retention window (#113).

    Featuring is operator-only, so the solves that survived the sweep were
    the ones the operator happened to notice in time; a set of six good
    frames from one traveler came in overnight and the best were gone
    before anyone looked. The person with the strongest claim on whether a
    photo stays up is the one who took it, and the result page is where
    they are when the decision is fresh. No account: the job id is the
    only access control there is (#21), and it is already the link.

    Same shape as /feature: every precondition rides in the UPDATE, so a
    concurrent /hide can't leave the job hidden *and* kept. Only inside
    the window, only a solved job, never a hidden one. Rate-limited per
    address so one person can't pin the whole feed."""
    if _keep_limited(_client_ip(request)):
        raise HTTPException(429, "that's enough kept for one day; try tomorrow")
    with db.get_conn() as conn:
        if not conn.execute(
            "UPDATE jobs SET kept = 1 WHERE id = ? AND hidden = 0 "
            "AND status = 'done' AND created_at >= datetime('now', ?)",
            (job_id, f"-{RETENTION_HOURS} hours"),
        ).rowcount:
            row = conn.execute(
                "SELECT status, hidden FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row or row["hidden"]:  # swept, never existed, or taken down
                raise HTTPException(404, _GONE)
            if row["status"] != "done":
                raise HTTPException(409, "only a solved photo can be kept")
            raise HTTPException(409, "the window to keep this solve has closed")
    return {"id": job_id, "kept": True}


@app.post("/jobs/{job_id}/unkeep")
def unkeep_job(job_id: str):
    """Withdraw a keep (#113). Past the window, the next sweep collects
    the job, which is what withdrawing means. Operator-featured jobs are
    untouched: `featured` is a separate flag."""
    with db.get_conn() as conn:
        if not conn.execute(
            "UPDATE jobs SET kept = 0 WHERE id = ? AND hidden = 0", (job_id,)
        ).rowcount:
            raise HTTPException(404, _GONE)
    return {"id": job_id, "kept": False}


FEED_LIMIT = 24


def _recent_solves():
    """The public "recently solved" list behind /feed and /feed.atom:
    successful solves across everyone, newest first, capped, for as long
    as retention keeps them. Hidden jobs (#60) never appear. The narration
    (#12) rides along when the worker produced one."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, result_json FROM jobs "
            "WHERE status = 'done' AND hidden = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (FEED_LIMIT,),
        ).fetchall()
    solves = []
    for row in rows:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        narration = result.get("narration") or {}
        solves.append({"id": row["id"], "created_at": row["created_at"],
                       "caption": narration.get("caption"),
                       "sky_tags": sky_tags.for_result(result),
                       "text": narration.get("text")})
    return solves


@app.get("/feed")
def feed():
    """The homepage's public "recently solved" strip. This deliberately
    makes recent solves discoverable — job links used to be unlisted —
    and the upload-page disclosure says so before anyone uploads. The
    caption is the thumbnail's alt text, sent only when there is one;
    sky_tags give the eye a hint of what part of the sky it shows."""
    jobs = []
    for solve in _recent_solves():
        entry = {"id": solve["id"], "created_at": solve["created_at"]}
        if solve["caption"]:
            entry["caption"] = solve["caption"]
        if solve["sky_tags"]:
            entry["sky_tags"] = solve["sky_tags"]
        jobs.append(entry)
    return {"jobs": jobs}


ATOM_NS = "http://www.w3.org/2005/Atom"
ATOM_TITLE = "asterism — recently solved"
ATOM_UNTITLED = "A solved night-sky photo"


def _rfc3339(created_at):
    # SQLite datetime('now') is "YYYY-MM-DD HH:MM:SS" UTC; Atom wants
    # RFC 3339. The same rewrite the homepage does before new Date().
    return created_at.replace(" ", "T") + "Z"


class AtomResponse(Response):
    # Declared on the route as well as returned from it, so /openapi.json
    # advertises the type the endpoint actually serves rather than the
    # JSON default. The XML prolog carries the encoding.
    media_type = "application/atom+xml"


@app.get("/feed.atom", response_class=AtomResponse)
def feed_atom(request: Request):
    """The strip as an Atom feed (#127): the one way to follow new solves
    that asks nothing of the reader — no account to notify, nothing to
    install. Same list and cap as /feed; the caption is the title, the
    share card (#13) rides inline and as an enclosure, and the entry
    links to the result page. Entries expire with retention, which is
    fine: a reader keeps what it fetched, and a featured solve simply
    stays valid. Built with ElementTree so captions and narration are
    escaped by something that knows XML, not by hand."""
    base = str(request.base_url).rstrip("/")
    solves = _recent_solves()
    root = ET.Element("feed", xmlns=ATOM_NS)
    ET.SubElement(root, "title").text = ATOM_TITLE
    ET.SubElement(root, "subtitle").text = (
        "Night-sky photos, plate-solved and labeled from their star patterns.")
    ET.SubElement(root, "id").text = f"{base}/feed.atom"
    ET.SubElement(root, "link", rel="self", type="application/atom+xml",
                  href=f"{base}/feed.atom")
    ET.SubElement(root, "link", rel="alternate", type="text/html",
                  href=f"{base}/")
    ET.SubElement(ET.SubElement(root, "author"), "name").text = "asterism"
    # Atom requires a feed-level <updated>; with nothing in the feed there
    # is nothing to date it by but now.
    ET.SubElement(root, "updated").text = (
        _rfc3339(solves[0]["created_at"]) if solves
        else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    for solve in solves:
        page = f"{base}/?job={solve['id']}"
        card_url = f"{base}/jobs/{solve['id']}/card"
        title = solve["caption"] or ATOM_UNTITLED
        when = _rfc3339(solve["created_at"])
        entry = ET.SubElement(root, "entry")
        ET.SubElement(entry, "id").text = page
        ET.SubElement(entry, "title").text = title
        ET.SubElement(entry, "link", rel="alternate", type="text/html",
                      href=page)
        ET.SubElement(entry, "link", rel="enclosure", type="image/png",
                      href=card_url)
        ET.SubElement(entry, "published").text = when
        ET.SubElement(entry, "updated").text = when
        # type="html": readers that ignore enclosures still show the card.
        body = (f'<p><img src="{html.escape(card_url)}" '
                f'alt="{html.escape(title)}"></p>')
        if solve["text"]:
            body += f"<p>{html.escape(solve['text'])}</p>"
        ET.SubElement(entry, "content", type="html").text = body
    return AtomResponse('<?xml version="1.0" encoding="utf-8"?>\n'
                        + ET.tostring(root, encoding="unicode"))


def _public_exif(exif_info):
    """Round GPS for the public payload: results are shareable by link, and
    precise coordinates are usually someone's backyard (#22). One decimal
    (~11 km) is plenty to say which planet was where; the worker keeps the
    full-precision copy in the job record."""
    if not exif_info:
        return exif_info
    out = dict(exif_info)
    # NaN survives a json.dumps/loads round trip but not the strict encoder
    # Starlette serves responses with, so a single non-finite value stored
    # before exif.py learned to reject them 500s the whole payload. Null it
    # here as well: this endpoint has to keep working for rows already in
    # the database.
    for key, value in out.items():
        if isinstance(value, float) and not math.isfinite(value):
            out[key] = None
    for key in ("lat", "lon"):
        if out.get(key) is not None:
            out[key] = round(out[key], 1)
    return out


def _queue_position(conn, row):
    """How many jobs run before this queued one: the one solving now plus
    queued jobs ahead in the worker's claim order — quick jobs before deep
    ones, FIFO (created_at, then id) within each class. Must stay in step
    with worker.claim_next_job, or the count shown to a waiting uploader
    drifts from the order jobs actually run."""
    deep = 1 if row["mode"] == "deep" else 0
    ahead = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE hidden = 0 AND (status = 'solving' "
        "OR (status = 'queued' AND ((mode = 'deep') < :d "
        "    OR ((mode = 'deep') = :d AND (created_at < :c "
        "        OR (created_at = :c AND id < :i))))))",
        {"d": deep, "c": row["created_at"], "i": row["id"]},
    ).fetchone()
    return ahead["n"]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT *, created_at >= datetime('now', ?) AS in_window "
            "FROM jobs WHERE id = ?",
            (f"-{RETENTION_HOURS} hours", job_id),
        ).fetchone()
        position = _queue_position(conn, row) if row and row["status"] == "queued" else None
    if not row or row["hidden"]:
        raise HTTPException(404, _GONE)
    out = {
        "id": row["id"],
        "status": row["status"],
        "error": row["error"],
        "solve_seconds": row["solve_seconds"],
        "exif": _public_exif(json.loads(row["exif_json"]) if row["exif_json"] else None),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        # Keep control (#113): whether this solve is kept, and whether the
        # page should offer to keep it (solved, and still inside the window).
        "kept": bool(row["kept"]),
        "keep_open": bool(row["status"] == "done" and row["in_window"]),
    }
    if position is not None:
        out["queue_position"] = position
    return out


@app.get("/jobs/{job_id}/image")
def get_job_image(job_id: str):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT image_path, hidden FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row or row["hidden"] or not os.path.exists(row["image_path"]):
        raise HTTPException(404, _GONE)
    return FileResponse(row["image_path"])


@app.get("/jobs/{job_id}/card")
def get_job_card(job_id: str, request: Request):
    """Share card (#13): the annotated photo as a PNG, rendered once per
    job and cached beside the upload (same retention sweep collects it)."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT image_path, status, result_json, hidden FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if not row or row["hidden"] or not os.path.exists(row["image_path"]):
        raise HTTPException(404, _GONE)
    if row["status"] != "done" or not row["result_json"]:
        raise HTTPException(409, "no card until the solve finishes")
    card_path = row["image_path"] + ".card.png"
    if not os.path.exists(card_path):
        card.render(row["image_path"], json.loads(row["result_json"]),
                    request.url.hostname or "asterism", card_path)
    return FileResponse(card_path, media_type="image/png",
                        filename=f"asterism-{job_id[:8]}.png")


# FastAPI registers only the method named on the decorator, so every GET
# route above answered HEAD with 405 (plain Starlette routes add HEAD for
# free). Feed readers HEAD an enclosure to learn its size before fetching
# it (#127), and a link checker HEADs a result page; nothing here is any
# different for HEAD, and the body is dropped downstream on its own.
for _route in app.routes:
    if isinstance(_route, APIRoute) and "GET" in _route.methods:
        _route.methods.add("HEAD")
