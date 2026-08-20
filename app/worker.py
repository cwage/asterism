"""Job worker: poll SQLite for queued solves, run them, write back results.
Also the retention sweeper: this is not a photo-hosting service (#23)."""

import json
import os
import shutil
import time
import traceback

from . import (constellations, db, dso, ephemeris, narrate, notify,
               satellites, solver, verify)

# Below this many detected star-like sources, a quick job fails fast
# instead of burning cpulimit tiers on daylight/food/pitch-black uploads.
PRECHECK_MIN_STARS = int(os.environ.get("PRECHECK_MIN_STARS", "10"))

RETENTION_HOURS = int(os.environ.get("RETENTION_HOURS", "24"))
SWEEP_INTERVAL_SECONDS = 900


def due(last, interval, now=None):
    """Whether a periodic task should run. `last is None` means never run,
    which is always due.

    The subtlety is the clock. time.monotonic() is measured from boot, and
    a Fly machine with auto_stop_machines is a fresh boot every time it
    wakes — measured at 85.9s on a machine that had been serving for
    minutes. So comparing against an initial 0.0 asks for a full interval
    of *continuous uptime* before the first run: 15 minutes for the sweep,
    10 for notifications, on a machine that stops whenever nobody is
    looking. On a quiet site neither would ever run.

    Starting from None instead runs both once per wake, which is the
    intended cadence anyway — the sweep is idempotent, and the
    notification watermarks live in the database precisely so they survive
    the stop.

    (Local docker hides this completely: containers share the host's
    monotonic clock, which is days large, so the first tick always fires.)
    """
    if last is None:
        return True
    return (time.monotonic() if now is None else now) - last > interval


def sweep_expired():
    """Delete jobs (rows, uploads, solve artifacts) older than the retention
    window. Returns how many were removed.

    Featured jobs (#67) are exempt: a handful of good solves are kept as
    permanent examples so the homepage feed isn't empty on a quiet day.
    Hiding a job clears the flag, so the kill switch (#60) always wins and
    nothing can be both invisible and immortal."""
    removed = 0
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, image_path FROM jobs "
            "WHERE created_at < datetime('now', ?) AND featured = 0",
            (f"-{RETENTION_HOURS} hours",),
        ).fetchall()
        for row in rows:
            # Delete first, re-checking featured, and only touch the bytes if
            # the row was actually ours to take. sqlite3 opens no transaction
            # for the SELECT above, so /feature can commit in the gap — and
            # unlink() has no transaction to roll back, so unlinking first
            # would destroy a job that had just been marked permanent. From
            # the first DELETE onward we hold the write lock, so nothing else
            # can interleave; a crash mid-loop rolls the deletes back and
            # leaves rows whose files are gone, which the next sweep collects.
            if not conn.execute(
                "DELETE FROM jobs WHERE id = ? AND featured = 0", (row["id"],)
            ).rowcount:
                continue
            removed += 1
            if row["image_path"]:
                # The share card (#13) is cached beside the upload.
                for path in (row["image_path"], row["image_path"] + ".card.png"):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
            shutil.rmtree(os.path.join(db.DATA_DIR, "jobs", row["id"]),
                          ignore_errors=True)
    return removed


def _col(job, name, default=None):
    """Column access that works for sqlite Rows and plain dicts alike."""
    try:
        val = job[name]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def _widths(attempts):
    return ", ".join(
        f"{a['fov_bounds'][0]:.0f}-{a['fov_bounds'][1]:.0f}deg" for a in attempts
    )


def _describe_failure(attempts):
    """(reason, message) for a solve that produced no usable WCS (#72).

    Running out of CPU and finishing a search empty-handed both used to report
    "no solution", which reads as "we looked and it isn't there" — misleading
    when the search never finished. Every failure in the first round of outside
    uploads was a timeout wearing that copy."""
    timed_out = [a for a in attempts if a.get("timed_out")]
    searched = [a for a in attempts if not a.get("timed_out")]

    if timed_out and searched:
        return "partial_timeout", (
            f"no solution at {_widths(searched)}; ran out of solve time at "
            f"{_widths(timed_out)}"
        )
    if timed_out:
        return "timeout", (
            f"ran out of solve time at {_widths(timed_out)} — the search never "
            "finished, so this scale hasn't been ruled out"
        )
    return "no_match", f"no solution (tried field widths: {_widths(attempts)})"


def _attach_guess(result, exif_info):
    """A failed solve still gets a best-effort 'here's what was up' answer
    from the ephemeris (#7). Never lets a guess failure mask the real result."""
    try:
        guess = ephemeris.fallback_guess(exif_info)
    except Exception:
        print(f"worker: fallback guess failed\n{traceback.format_exc()}")
        guess = None
    if guess:
        result["failure"]["guess"] = guess
        return
    # Silence is the worst outcome and the one that reads like a bug, so say
    # which piece of missing EXIF stopped us (#82).
    try:
        reason = ephemeris.guess_unavailable_reason(exif_info)
    except Exception:
        reason = None
    result["failure"]["guess_unavailable"] = reason or "unavailable"


def _label_everything(result, wcs_path, image_path, exif_info, job_id):
    """Project every layer through a WCS and verify it against the pixels."""
    labels = solver.annotate(
        wcs_path, exif_info["width"], exif_info["height"]
    )

    # Ephemeris labels are best-effort extras: never fail a good solve on them.
    try:
        bodies, eph_meta = ephemeris.annotate_bodies(
            wcs_path, exif_info["width"], exif_info["height"], exif_info
        )
    except Exception:
        # Full traceback stays in the worker log; clients get a stable schema.
        print(f"worker: ephemeris failed for {job_id}\n{traceback.format_exc()}")
        bodies, eph_meta = [], {"time_utc": None, "time_source": None,
                                "error": "ephemeris computation failed"}

    # Constellation figures ride the same best-effort rule.
    try:
        figures = constellations.annotate(
            wcs_path, exif_info["width"], exif_info["height"]
        )
    except Exception:
        print(f"worker: constellations failed for {job_id}\n{traceback.format_exc()}")
        figures = []

    # Naked-eye deep-sky objects (#16), best-effort like the layers above.
    try:
        dsos = dso.annotate(
            wcs_path, exif_info["width"], exif_info["height"]
        )
    except Exception:
        print(f"worker: dso annotation failed for {job_id}\n{traceback.format_exc()}")
        dsos = []

    labels = bodies + labels + dsos

    # Verification closes the loop against the pixels (issue #28): snap
    # labels to detected sources, flag cloud-hidden stars, correct for
    # stack warp. Best-effort like the layers above.
    try:
        labels, figures, verification = verify.apply(
            image_path, labels, figures
        )
    except Exception:
        print(f"worker: verification failed for {job_id}\n{traceback.format_exc()}")
        verification = {"verified": False, "error": "verification failed"}

    # Satellite crossings during the exposure (#11), best-effort: needs a
    # timestamp, GPS, and Space-Track credentials, and reports why not
    # when it can't run.
    try:
        sats = satellites.annotate(
            wcs_path, exif_info["width"], exif_info["height"],
            exif_info
        )
    except Exception:
        print(f"worker: satellites failed for {job_id}\n{traceback.format_exc()}")
        sats = {"skipped": "satellite lookup failed"}

    result["labels"] = labels
    result["ephemeris"] = eph_meta
    result["constellations"] = figures
    result["verification"] = verification
    result["satellites"] = sats

    # LLM narration (#12), best-effort: no API key or a failed call just
    # leaves the deterministic card caption in place.
    try:
        narration = narrate.annotate(result)
        if narration:
            result["narration"] = narration
    except Exception:
        print(f"worker: narration failed for {job_id}\n{traceback.format_exc()}")

    return result

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
            _attach_guess(result, exif_info)
            return "failed", result, (
                f"only {n} star-like sources detected — cloudy, daylight, "
                "or not a sky photo"
            )
        # Checkpoint 2: quick mode tries the EXIF-derived tiers — the
        # uncropped bracket plus the sensor-crop extension, so a hidden-crop
        # phone shot (#57) solves without a "try harder" click. Without
        # EXIF, just the most likely fallback.
        tiers = plan[:len(solver.exif_tiers(exif_info))] or plan[:1]
    else:
        # Deep mode: whatever the quick pass didn't already try with the
        # full budget. Quick attempts run trimmed (thorough=False, see
        # solve_tiered) and must be re-run, not skipped; records from
        # before the flag existed were full-budget runs, hence the True
        # default.
        prior = json.loads(_col(job, "result_json") or "{}")
        tried = {tuple(a["fov_bounds"]) for a in prior.get("attempts", [])
                 if a.get("thorough", True)}
        tiers = [t for t in plan
                 if (round(t[0], 1), round(t[1], 1)) not in tried]
        if (prior.get("failure") or {}).get("reason") == "no_stars":
            # The gate returns before the solver runs, so `attempts` is empty
            # and nothing looks tried — the whole plan would run. But scale
            # tiers answer "how wide is this field", not "are there stars in
            # it": if the detector found four sources, no tier is going to
            # find a quad among them. Honour the override, at one tier rather
            # than four (#90). Measured cost of getting this wrong: a job that
            # could not succeed held the single-worker queue for 20 minutes.
            tiers = tiers[:1]

    result = solver.solve_tiered(job["image_path"], out_dir, exif_info,
                                 tiers=tiers, quick=(mode != "deep"))
    if mode == "deep":
        # Keep the quick pass's attempts visible in the final record.
        prior = json.loads(_col(job, "result_json") or "{}")
        result["attempts"] = prior.get("attempts", []) + result["attempts"]
        result["total_seconds"] = round(
            result["total_seconds"] + (prior.get("total_seconds") or 0), 2)

    if not result["success"]:
        # Only full-budget attempts retire a tier; trimmed quick attempts
        # leave the whole plan open to a deeper run.
        remaining = len(plan) - sum(
            1 for a in result["attempts"] if a.get("thorough", True))
        reason, message = _describe_failure(result["attempts"])
        result["failure"] = {"reason": reason,
                             "can_deepen": mode != "deep" and remaining > 0}
        _attach_guess(result, exif_info)
        return "failed", result, message

    result = _label_everything(result, result["wcs_path"],
                               job["image_path"], exif_info, job["id"])
    return "done", result, None


MAX_ORPHAN_RECOVERIES = 2


def recover_orphans():
    """Re-queue 'solving' rows at startup. We are the only worker, so any
    such row is an orphan from a previous life (Fly auto-stop mid-solve, a
    crash) — left alone it would count against the queue-depth cap until
    retention reaped it. A job that keeps getting orphaned is probably the
    thing *causing* the crashes (an image whose solve OOMs the VM turned one
    bad upload into a machine boot loop), so after MAX_ORPHAN_RECOVERIES
    re-queues it is failed instead of retried. Returns (requeued, abandoned)
    counts."""
    with db.get_conn() as conn:
        abandoned = conn.execute(
            "UPDATE jobs SET status = 'failed', "
            "error = 'solve was interrupted repeatedly; not retrying' "
            "WHERE status = 'solving' AND orphan_recoveries >= ?",
            (MAX_ORPHAN_RECOVERIES,),
        ).rowcount
        requeued = conn.execute(
            "UPDATE jobs SET status = 'queued', "
            "orphan_recoveries = orphan_recoveries + 1 "
            "WHERE status = 'solving'"
        ).rowcount
    return requeued, abandoned


def claim_next_job(conn):
    """Take the next queued job and mark it 'solving'. Returns the row, or
    None if there was nothing to take.

    Quick jobs always run before deep ones: a deep solve is the slowest job
    type with the worst odds, and on a single worker equal priority lets one
    person's lost cause hold every fresh upload hostage for minutes (measured
    2026-08-19: a five-tier deep solve blocked three quick jobs, each of
    which would have finished in seconds). A deep job therefore only gets
    the worker when the quick queue is empty; it can be pushed back
    repeatedly, which is the right trade — its uploader already got a
    verdict and chose to wait, while quick uploaders are staring at a queue.

    The id tiebreak keeps FIFO deterministic when created_at (second
    resolution) collides, and matches the API's queue-position math.
    hidden = 0 keeps a job pulled by the kill switch (#60) from burning a
    solve on its way to the retention sweep.

    The UPDATE re-checks both preconditions rather than trusting the SELECT:
    sqlite3 opens no transaction for a SELECT and WAL readers don't block
    writers, so a /hide can commit between the two statements. An
    unconditional claim would go on to solve a job that is already hidden."""
    job = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' AND hidden = 0 "
        "ORDER BY mode = 'deep', created_at, id LIMIT 1"
    ).fetchone()
    if not job:
        return None
    claimed = conn.execute(
        "UPDATE jobs SET status = 'solving' "
        "WHERE id = ? AND status = 'queued' AND hidden = 0",
        (job["id"],),
    ).rowcount
    return job if claimed else None


def main():
    db.init_db()
    requeued, abandoned = recover_orphans()
    if requeued:
        print(f"worker: re-queued {requeued} orphaned solving job(s)")
    if abandoned:
        print(f"worker: abandoned {abandoned} repeatedly-orphaned job(s)")
    print("worker: polling for jobs")
    # None, not 0.0: see due() — on a machine that stops when idle, 0.0
    # means "wait for a full interval of uptime before the first run".
    last_sweep = None
    last_notify = None
    while True:
        if due(last_sweep, SWEEP_INTERVAL_SECONDS):
            last_sweep = time.monotonic()
            try:
                n = sweep_expired()
                if n:
                    print(f"worker: retention sweep removed {n} expired job(s)")
            except Exception:
                print(f"worker: retention sweep failed\n{traceback.format_exc()}")

        if notify.enabled() and due(last_notify, notify.TICK_INTERVAL_SECONDS):
            last_notify = time.monotonic()
            try:
                with db.get_conn() as conn:
                    for message in notify.tick(conn):
                        print(f"worker: notified — {message}")
            except Exception:
                # notify.tick already swallows per-check failures; this
                # catches the connection itself. A dead ntfy, or a dead
                # anything here, must not stop solves (#69).
                print(f"worker: notify tick failed\n{traceback.format_exc()}")

        with db.get_conn() as conn:
            job = claim_next_job(conn)
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
