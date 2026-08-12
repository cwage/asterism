import json
import os
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from . import db, exif

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

_upload_log = defaultdict(deque)  # client ip -> recent upload monotonic times


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
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'solving')"
        ).fetchone()
    return row["n"]


@app.get("/")
def index():
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

    job_id = uuid.uuid4().hex[:12]
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
            "SELECT status, mode FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "no such job")
        if row["status"] != "failed" or row["mode"] == "deep":
            raise HTTPException(409, "job is not eligible for a deeper solve")
        # result_json is kept: the worker skips tiers the quick pass tried.
        conn.execute(
            "UPDATE jobs SET status = 'queued', mode = 'deep', error = NULL "
            "WHERE id = ?", (job_id,),
        )
    return {"id": job_id, "status": "queued", "mode": "deep"}


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
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'solving' "
        "OR (status = 'queued' AND (created_at < :c "
        "    OR (created_at = :c AND id < :i)))",
        {"c": row["created_at"], "i": row["id"]},
    ).fetchone()
    return ahead["n"]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        position = _queue_position(conn, row) if row and row["status"] == "queued" else None
    if not row:
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
            "SELECT image_path FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row or not os.path.exists(row["image_path"]):
        raise HTTPException(404, _GONE)
    return FileResponse(row["image_path"])
