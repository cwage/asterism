"""Activity notifications to ntfy.sh (#69): a roughly-nightly summary and a
burst alert when uploads arrive faster than this site normally sees them.

The purpose is volume awareness, not abuse forensics. The question being
answered is "is that fifteen real people finding the site, or one person I
need to reach for #60 about" — and the answer comes from going and looking,
which is what the notification is for. Nothing here tries to compute it.

Best-effort like narration (#12) and satellites (#11): `NTFY_TOPIC_URL`
unset means the feature is off, and a dead ntfy must never stop solves.

The topic URL is a credential — anyone holding it can both read the
notifications and publish to them — so it is never logged, not even on
failure.
"""

import json
import os
import traceback
import urllib.error
import urllib.request

def _int_env(name, default):
    """Set-but-empty counts as unset. `${NTFY_TICK_SECONDS:-}` in a compose
    file passes an empty string, and int("") at import time would take the
    worker down over a knob nobody turned."""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"notify: ignoring unreadable {name}, using {default}")
        return default


TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

# Deliberately low. On a site that normally sees single digits a day, a
# handful inside an hour is already the thing worth looking at.
BURST_SOLVES = _int_env("NTFY_BURST_SOLVES", 6)
BURST_WINDOW_MINUTES = _int_env("NTFY_BURST_WINDOW_MINUTES", 60)

# UTC, because the machine has no opinion about local time and the summary
# is "roughly nightly" regardless (see the wake-gap note in #69).
SUMMARY_HOUR_UTC = _int_env("NTFY_SUMMARY_HOUR_UTC", 7)
SUMMARY_WINDOW_HOURS = 24

TICK_INTERVAL_SECONDS = _int_env("NTFY_TICK_SECONDS", 600)
TIMEOUT_SECONDS = 10.0

# Watermarks live in `meta` rather than in the worker process: the machine
# stops on an idle night (auto_stop_machines), so anything held in memory
# is lost exactly when the gap is longest.
LAST_BURST_KEY = "notify_last_burst_at"
LAST_SUMMARY_KEY = "notify_last_summary_date"


def enabled():
    return bool(TOPIC_URL)


def post(message, title=None, tags=None, priority=None):
    """Publish one notification. Returns True if ntfy accepted it.

    Never raises: every caller is on a path whose actual job is solving
    photos, and a notification is not worth failing that for.
    """
    if not TOPIC_URL:
        return False
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = ",".join(tags)
    if priority:
        headers["Priority"] = str(priority)
    request = urllib.request.Request(
        TOPIC_URL, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception:
        # No URL in the log line: the traceback from urllib carries it, so
        # the message is written by hand rather than re-raised or printed.
        print("notify: ntfy publish failed (URL withheld)")
        return False


def _get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _reason(result_json):
    try:
        return ((json.loads(result_json or "{}").get("failure") or {})
                .get("reason"))
    except (ValueError, AttributeError):
        return None


def activity_counts(conn, since):
    """What the site did since `since` (a UTC 'YYYY-MM-DD HH:MM:SS' string).

    Undercounts by whatever the retention sweep already removed — rows are
    the only record, so a window longer than RETENTION_HOURS cannot be
    complete. Accepted in #69 rather than solved: the burst alert, which is
    the one that has to be right, fires while the traffic is happening.
    """
    rows = conn.execute(
        "SELECT status, result_json, hidden FROM jobs WHERE created_at >= ?",
        (since,)).fetchall()
    counts = {"uploads": len(rows), "solved": 0, "failed": 0, "hidden": 0,
              "reasons": {}}
    for row in rows:
        if row["status"] == "done":
            counts["solved"] += 1
        elif row["status"] == "failed":
            counts["failed"] += 1
            reason = _reason(row["result_json"])
            if reason:
                counts["reasons"][reason] = counts["reasons"].get(reason, 0) + 1
        if row["hidden"]:
            counts["hidden"] += 1
    # Featured is a running total, not a window count: the point of the flag
    # is the jobs that outlive the window, so "how many are there now" is
    # the only reading of it that means anything.
    counts["featured"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE featured = 1").fetchone()[0]
    return counts


def format_summary(counts):
    parts = [f"{counts['uploads']} uploads", f"{counts['solved']} solved"]
    failed = f"{counts['failed']} failed"
    if counts["reasons"]:
        worst = sorted(counts["reasons"].items(), key=lambda kv: -kv[1])
        failed += " (" + ", ".join(f"{n} {reason}" for reason, n in worst) + ")"
    parts.append(failed)
    parts.append(f"{counts['hidden']} hidden")
    parts.append(f"{counts['featured']} featured")
    return " · ".join(parts)


def _utc_now(conn):
    """Read the clock through SQLite, so the comparison and the value being
    compared come from the same place — the created_at column is written by
    SQLite's own datetime('now')."""
    return conn.execute("SELECT datetime('now')").fetchone()[0]


def _shift(conn, stamp, modifier):
    return conn.execute("SELECT datetime(?, ?)", (stamp, modifier)).fetchone()[0]


def check_burst(conn, now):
    """Alert when solves since the last alert exceed the threshold.

    Counts from the later of the burst window and the last alert, so one
    busy hour produces one notification rather than one per tick.
    """
    floor = _shift(conn, now, f"-{BURST_WINDOW_MINUTES} minutes")
    watermark = _get(conn, LAST_BURST_KEY)
    if watermark and watermark > floor:
        floor = watermark
    n = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE created_at > ? AND status = 'done'",
        (floor,)).fetchone()[0]
    if n < BURST_SOLVES:
        return None
    minutes = BURST_WINDOW_MINUTES
    message = f"{n} solves in the last {minutes} minutes"
    if post(message, title="asterism: busy", tags=["telescope"], priority=4):
        # Only advance on a delivered notification: if ntfy was down, the
        # next tick should still be able to tell someone about this burst.
        _set(conn, LAST_BURST_KEY, now)
    return message


def check_summary(conn, now):
    """Send one summary per UTC day, on the first tick at or after the
    configured hour. Drifts later if the machine was asleep — which only
    happens on a day quiet enough for the summary to read `0 uploads`."""
    today, clock = now.split(" ")
    if int(clock[:2]) < SUMMARY_HOUR_UTC:
        return None
    if _get(conn, LAST_SUMMARY_KEY) == today:
        return None
    since = _shift(conn, now, f"-{SUMMARY_WINDOW_HOURS} hours")
    message = format_summary(activity_counts(conn, since))
    if post(message, title="asterism: yesterday", tags=["bar_chart"]):
        _set(conn, LAST_SUMMARY_KEY, today)
    return message


def tick(conn):
    """Evaluate both triggers. Returns what was sent, for the worker log."""
    if not enabled():
        return []
    now = _utc_now(conn)
    sent = []
    for check in (check_burst, check_summary):
        try:
            message = check(conn, now)
        except Exception:
            print(f"notify: {check.__name__} failed\n{traceback.format_exc()}")
            continue
        if message:
            sent.append(message)
    return sent
