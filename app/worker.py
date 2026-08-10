"""Job worker: poll SQLite for queued solves, run them, write back results."""

import json
import os
import time
import traceback

from . import db, solver


def process(job):
    exif_info = json.loads(job["exif_json"])
    out_dir = os.path.join(db.DATA_DIR, "jobs", job["id"])
    result = solver.solve_tiered(job["image_path"], out_dir, exif_info)

    if not result["success"]:
        tried = ", ".join(
            f"{a['fov_bounds'][0]:.0f}-{a['fov_bounds'][1]:.0f}deg"
            for a in result["attempts"]
        )
        return "failed", result, f"no solution (tried field widths: {tried})"

    labels = solver.annotate(
        result["wcs_path"], exif_info["width"], exif_info["height"]
    )
    result["labels"] = labels
    return "done", result, None


def main():
    db.init_db()
    print("worker: polling for jobs")
    while True:
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
