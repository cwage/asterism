"""Job worker: poll SQLite for queued solves, run them, write back results.
Also the retention sweeper: this is not a photo-hosting service (#23)."""

import json
import os
import shutil
import time
import traceback

from . import constellations, db, ephemeris, solver, verify

# Below this many detected star-like sources, a quick job fails fast
# instead of burning cpulimit tiers on daylight/food/pitch-black uploads.
PRECHECK_MIN_STARS = int(os.environ.get("PRECHECK_MIN_STARS", "10"))

RETENTION_HOURS = int(os.environ.get("RETENTION_HOURS", "24"))
SWEEP_INTERVAL_SECONDS = 900


def sweep_expired():
    """Delete jobs (rows, uploads, solve artifacts) older than the retention
    window. Returns how many were removed."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, image_path FROM jobs WHERE created_at < datetime('now', ?)",
            (f"-{RETENTION_HOURS} hours",),
        ).fetchall()
        for row in rows:
            if row["image_path"]:
                try:
                    os.unlink(row["image_path"])
                except FileNotFoundError:
                    pass
            shutil.rmtree(os.path.join(db.DATA_DIR, "jobs", row["id"]),
                          ignore_errors=True)
            conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
    return len(rows)


def _col(job, name, default=None):
    """Column access that works for sqlite Rows and plain dicts alike."""
    try:
        val = job[name]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def process(job):
    exif_info = json.loads(job["exif_json"])
    out_dir = os.path.join(db.DATA_DIR, "jobs", job["id"])
    mode = _col(job, "mode", "quick")
    plan = solver.tier_plan(exif_info)

    if mode != "deep":
        # Checkpoint 1: don't invoke the solver at all on zero-star images.
        n = verify.count_stars(job["image_path"])
        if n is not None and n < PRECHECK_MIN_STARS:
            result = {"success": False, "attempts": [], "total_seconds": 0.0,
                      "failure": {"reason": "no_stars", "stars_detected": n,
                                  "can_deepen": True}}
            return "failed", result, (
                f"only {n} star-like sources detected — cloudy, daylight, "
                "or not a sky photo"
            )
        # Checkpoint 2: quick mode tries only the most likely scale tier.
        tiers = plan[:1]
    else:
        # Deep mode: whatever the quick pass didn't already try.
        prior = json.loads(_col(job, "result_json") or "{}")
        tried = {tuple(a["fov_bounds"]) for a in prior.get("attempts", [])}
        tiers = [t for t in plan
                 if (round(t[0], 1), round(t[1], 1)) not in tried]

    result = solver.solve_tiered(job["image_path"], out_dir, exif_info,
                                 tiers=tiers)
    if mode == "deep":
        # Keep the quick pass's attempts visible in the final record.
        prior = json.loads(_col(job, "result_json") or "{}")
        result["attempts"] = prior.get("attempts", []) + result["attempts"]
        result["total_seconds"] = round(
            result["total_seconds"] + (prior.get("total_seconds") or 0), 2)

    if not result["success"]:
        tried = ", ".join(
            f"{a['fov_bounds'][0]:.0f}-{a['fov_bounds'][1]:.0f}deg"
            for a in result["attempts"]
        )
        remaining = len(plan) - len(result["attempts"])
        result["failure"] = {"reason": "no_match",
                             "can_deepen": mode != "deep" and remaining > 0}
        return "failed", result, f"no solution (tried field widths: {tried})"

    labels = solver.annotate(
        result["wcs_path"], exif_info["width"], exif_info["height"]
    )

    # Ephemeris labels are best-effort extras: never fail a good solve on them.
    try:
        bodies, eph_meta = ephemeris.annotate_bodies(
            result["wcs_path"], exif_info["width"], exif_info["height"], exif_info
        )
    except Exception:
        # Full traceback stays in the worker log; clients get a stable schema.
        print(f"worker: ephemeris failed for {job['id']}\n{traceback.format_exc()}")
        bodies, eph_meta = [], {"time_utc": None, "time_source": None,
                                "error": "ephemeris computation failed"}

    # Constellation figures ride the same best-effort rule.
    try:
        figures = constellations.annotate(
            result["wcs_path"], exif_info["width"], exif_info["height"]
        )
    except Exception:
        print(f"worker: constellations failed for {job['id']}\n{traceback.format_exc()}")
        figures = []

    labels = bodies + labels

    # Verification closes the loop against the pixels (issue #28): snap
    # labels to detected sources, flag cloud-hidden stars, correct for
    # stack warp. Best-effort like the layers above.
    try:
        labels, figures, verification = verify.apply(
            job["image_path"], labels, figures
        )
    except Exception:
        print(f"worker: verification failed for {job['id']}\n{traceback.format_exc()}")
        verification = {"verified": False, "error": "verification failed"}

    result["labels"] = labels
    result["ephemeris"] = eph_meta
    result["constellations"] = figures
    result["verification"] = verification
    return "done", result, None


def main():
    db.init_db()
    print("worker: polling for jobs")
    last_sweep = 0.0
    while True:
        if time.monotonic() - last_sweep > SWEEP_INTERVAL_SECONDS:
            last_sweep = time.monotonic()
            try:
                n = sweep_expired()
                if n:
                    print(f"worker: retention sweep removed {n} expired job(s)")
            except Exception:
                print(f"worker: retention sweep failed\n{traceback.format_exc()}")

        with db.get_conn() as conn:
            job = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if job:
                conn.execute(
                    "UPDATE jobs SET status = 'solving' WHERE id = ?", (job["id"],)
                )
        if not job:
            time.sleep(1)
            continue

        print(f"worker: solving {job['id']}")
        try:
            status, result, error = process(job)
        except Exception:
            status, result, error = "failed", None, traceback.format_exc()[-2000:]

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, result_json = ?, error = ?, solve_seconds = ? "
                "WHERE id = ?",
                (
                    status,
                    json.dumps(result) if result else None,
                    error,
                    result.get("total_seconds") if result else None,
                    job["id"],
                ),
            )
        print(f"worker: {job['id']} -> {status}")


if __name__ == "__main__":
    main()
