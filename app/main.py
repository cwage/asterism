import hmac
import json
import os
import re
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from . import card, db, exif

app = FastAPI(title="asterism")
db.init_db()

UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Abuse limits (#10): every accepted upload is worker CPU (worst case ~200s
# for an unsolvable image in deep mode), so the open endpoint gets caps.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
UPLOADS_PER_HOUR = int(os.environ.get("UPLOADS_PER_HOUR", "12"))
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


def _rate_limited(ip, now=None):
    """Sliding one-hour window per client IP. Counts attempts, not successes,
    so a rejected upload isn't a free retry."""
    now = time.monotonic() if now is None else now
    log = _upload_log[ip]
    while log and log[0] <= now - 3600:
        log.popleft()
    if len(log) >= UPLOADS_PER_HOUR:
        return True
    log.append(now)
    if len(_upload_log) > 10000:  # shed empty entries under IP churn
        for key in [k for k, v in _upload_log.items() if not v][:5000]:
            del _upload_log[key]
    return False


def _queue_depth():
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE status IN ('queued', 'solving') AND hidden = 0"
        ).fetchone()
    return row["n"]


@app.get("/")
def index(request: Request, job: str | None = None):
    # Share links (?job=...) get OpenGraph tags pointing at the rendered
    # card (#13) so unfurls show the annotated photo. Job ids are uuid4
    # hex; anything else is served untouched (the frontend handles bad ids).
    if job and re.fullmatch(r"[0-9a-f]{32}", job):
        with open("static/index.html") as f:
            html = f.read()
        base = str(request.base_url).rstrip("/")
        meta = (
            '<meta property="og:title" content="asterism — what you saw">\n'
            '<meta property="og:description" content="A night-sky photo, '
            'plate-solved and labeled from its star pattern.">\n'
            f'<meta property="og:image" content="{base}/jobs/{job}/card">\n'
            '<meta name="twitter:card" content="summary_large_image">\n'
        )
        return HTMLResponse(html.replace("</head>", meta + "</head>"))
    return FileResponse("static/index.html")


@app.post("/jobs")
async def create_job(request: Request, image: UploadFile):
    if _rate_limited(_client_ip(request)):
        raise HTTPException(429, "rate limit: try again in a bit")
    if _queue_depth() >= MAX_QUEUE_DEPTH:
        raise HTTPException(503, "solve queue is full: try again in a few minutes")

    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"image too large (max {MAX_UPLOAD_BYTES // (1024*1024)}MB)")

    # Full 128 bits: the result URL is the only access control (#21).
    job_id = uuid.uuid4().hex
    ext = os.path.splitext(image.filename or "")[1].lower() or ".jpg"
    image_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")
    with open(image_path, "wb") as f:
        f.write(data)

    try:
        exif_info = exif.read_exif(image_path)
    except Exception as e:
        os.unlink(image_path)
        raise HTTPException(400, f"could not read image: {e}")

    # Precise GPS is captured into the job record above (the ephemeris layer
    # wants it); the stored file is served publicly, so scrub it (#22).
    # Fail closed: if the image carries GPS we cannot strip (non-JPEG or a
    # piexif failure), reject rather than serve location data back out.
    try:
        exif.strip_gps(image_path)
    except Exception:
        if exif_info.get("lat") is not None or exif_info.get("lon") is not None:
            os.unlink(image_path)
            raise HTTPException(
                415, "this image format carries GPS metadata we can't remove; "
                     "strip location data and re-upload"
            )

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, image_path, exif_json) VALUES (?, ?, ?)",
            (job_id, image_path, json.dumps(exif_info)),
        )
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


# Bodies a person can be asked to point at. The Moon is the one that makes
# identification possible at all — it is unmistakable to a human and, per #85,
# routinely mistaken for a porch light by software.
ANCHOR_NAMES = {"Moon", "Venus", "Jupiter", "Mars", "Saturn", "Mercury"}


@app.post("/jobs/{job_id}/anchor")
async def anchor_job(job_id: str, request: Request):
    """Register a failed job from two objects the uploader pointed at (#85).

    Takes {"anchors": [{"name": "Moon", "x": .., "y": ..}, ...]} in image
    pixels. Taps are snapped to the nearest detected source by the worker, so
    they only have to be close.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "expected a JSON body")
    anchors = payload.get("anchors") if isinstance(payload, dict) else None
    if not isinstance(anchors, list) or len(anchors) != 2:
        raise HTTPException(400, "give exactly two anchors")

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, hidden, exif_json FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row or row["hidden"]:
            raise HTTPException(404, _GONE)
        if row["status"] != "failed":
            raise HTTPException(409, "only a failed job can be placed by hand")

        info = json.loads(row["exif_json"] or "{}")
        width, height = info.get("width") or 0, info.get("height") or 0
        clean = []
        names = set()
        for anchor in anchors:
            if not isinstance(anchor, dict):
                raise HTTPException(400, "each anchor needs a name and a position")
            name = str(anchor.get("name", ""))
            if name not in ANCHOR_NAMES:
                raise HTTPException(400, f"unknown object: {name[:20]}")
            try:
                x, y = float(anchor["x"]), float(anchor["y"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "each anchor needs numeric x and y")
            if not (0 <= x < width and 0 <= y < height):
                raise HTTPException(400, "anchor is outside the photo")
            names.add(name)
            clean.append({"name": name, "x": x, "y": y})
        if len(names) != 2:
            raise HTTPException(400, "the two anchors must be different objects")

        conn.execute(
            "UPDATE jobs SET status = 'queued', mode = 'anchors', error = NULL, "
            "anchors_json = ? WHERE id = ?", (json.dumps(clean), job_id),
        )
    return {"id": job_id, "status": "queued", "mode": "anchors"}


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
        # featured = 0 as well: the kill switch outranks the showcase (#67).
        # Leaving both set would strand a job that is invisible *and* exempt
        # from the sweep, so its bytes would never leave the disk.
        if not conn.execute(
            "UPDATE jobs SET hidden = 1, featured = 0 WHERE id = ?", (job_id,)
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


FEED_LIMIT = 24


@app.get("/feed")
def feed():
    """The homepage's public "recently solved" strip: successful solves
    across everyone, newest first, for as long as retention keeps them.
    This deliberately makes recent solves discoverable — job links used
    to be unlisted — and the upload-page disclosure says so before anyone
    uploads. The narration caption (#12) rides along as alt text when the
    worker produced one. Hidden jobs (#60) never appear."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, result_json FROM jobs "
            "WHERE status = 'done' AND hidden = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (FEED_LIMIT,),
        ).fetchall()
    jobs = []
    for row in rows:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        entry = {"id": row["id"], "created_at": row["created_at"]}
        caption = (result.get("narration") or {}).get("caption")
        if caption:
            entry["caption"] = caption
        jobs.append(entry)
    return {"jobs": jobs}


def _public_exif(exif_info):
    """Round GPS for the public payload: results are shareable by link, and
    precise coordinates are usually someone's backyard (#22). One decimal
    (~11 km) is plenty to say which planet was where; the worker keeps the
    full-precision copy in the job record."""
    if not exif_info:
        return exif_info
    out = dict(exif_info)
    for key in ("lat", "lon"):
        if out.get(key) is not None:
            out[key] = round(out[key], 1)
    return out


def _queue_position(conn, row):
    """How many jobs run before this queued one: the one solving now plus
    queued jobs ahead in FIFO order (created_at, then id — the same order
    the worker consumes)."""
    ahead = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE hidden = 0 AND (status = 'solving' "
        "OR (status = 'queued' AND (created_at < :c "
        "    OR (created_at = :c AND id < :i))))",
        {"c": row["created_at"], "i": row["id"]},
    ).fetchone()
    return ahead["n"]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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
