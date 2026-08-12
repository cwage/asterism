import json
import os
import uuid

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import db, exif

app = FastAPI(title="asterism")
db.init_db()

UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/jobs")
async def create_job(image: UploadFile):
    job_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(image.filename or "")[1].lower() or ".jpg"
    image_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")
    with open(image_path, "wb") as f:
        f.write(await image.read())

    try:
        exif_info = exif.read_exif(image_path)
    except Exception as e:
        os.unlink(image_path)
        raise HTTPException(400, f"could not read image: {e}")

    # Precise GPS is captured into the job record above (the ephemeris layer
    # wants it); the stored file is served publicly, so scrub it (#22).
    # Best-effort: non-JPEG uploads fall through to the API-level redaction.
    try:
        exif.strip_gps(image_path)
    except Exception:
        pass

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


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such job")
    out = {
        "id": row["id"],
        "status": row["status"],
        "error": row["error"],
        "solve_seconds": row["solve_seconds"],
        "exif": _public_exif(json.loads(row["exif_json"]) if row["exif_json"] else None),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }
    return out


@app.get("/jobs/{job_id}/image")
def get_job_image(job_id: str):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT image_path FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row or not os.path.exists(row["image_path"]):
        raise HTTPException(404, "no such job")
    return FileResponse(row["image_path"])
