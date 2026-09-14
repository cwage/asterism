"""Who uploaded what, without keeping who (#116).

The client address is the only thing that ties one upload to another, and
it is also the one thing this site should never become a record of. So it
is never stored. What is stored is an HMAC of it under a salt that rotates
at UTC midnight and is destroyed once its day is over: uploads from one
address on one day share a hash, and nothing — not even the database — can
turn that hash back into an address or join it across days. That answers
"was that burst one person?" and "how many different people used the site
yesterday?", which the bare counts never could.

The counts themselves used to die with the rows: the retention sweep
deleted the only record, so a summary window longer than RETENTION_HOURS
undercounted (#69). Now the sweep rolls every row into `daily_stats` before
deleting it, keyed by the UTC day the job was created, and remembers the
day's distinct uploader hashes in `daily_uploaders`. Featured rows are never
deleted, so `jobs.counted` marks the ones already folded in.

None of this reaches a public payload: `get_job` and the feed build theirs
field by field.
"""

import hashlib
import hmac
import json
import secrets

SALT_PREFIX = "salt:"
# 64 bits of a 256-bit HMAC: collisions between the handful of addresses
# the site sees in a day are not a concern, and a short token reads better
# in a query result.
HASH_CHARS = 16


def _today(conn, now=None):
    """UTC date, read through SQLite so it matches created_at's clock."""
    if now is None:
        now = conn.execute("SELECT datetime('now')").fetchone()[0]
    return now[:10]


def _salt(conn, day):
    key = SALT_PREFIX + day
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    # OR IGNORE: if two requests race to mint the day's salt, both end up
    # reading whichever insert won, so their hashes agree.
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                 (key, secrets.token_hex(32)))
    return conn.execute("SELECT value FROM meta WHERE key = ?",
                        (key,)).fetchone()["value"]


def uploader_hash(conn, ip, now=None):
    """Today's token for a client address, or None without one."""
    if not ip:
        return None
    salt = bytes.fromhex(_salt(conn, _today(conn, now)))
    digest = hmac.new(salt, ip.encode("utf-8", "replace"), hashlib.sha256)
    return digest.hexdigest()[:HASH_CHARS]


def retire_salts(conn, now=None):
    """Delete the salts of past days. No hash for a past day is ever
    computed again, and a surviving salt is the only way one could be
    reversed. Returns how many were removed."""
    return conn.execute(
        "DELETE FROM meta WHERE key LIKE ? AND key < ?",
        (SALT_PREFIX + "%", SALT_PREFIX + _today(conn, now)),
    ).rowcount


def failure_reason(result_json):
    try:
        return ((json.loads(result_json or "{}").get("failure") or {})
                .get("reason"))
    except (ValueError, AttributeError):
        return None


def roll_up(conn, cutoff_modifier):
    """Fold every not-yet-counted job older than the cutoff (a SQLite
    datetime modifier such as '-24 hours') into the daily history. Runs
    inside the sweep's transaction, ahead of its deletes, so a row is
    either counted and gone or neither. Returns how many were counted."""
    rows = conn.execute(
        "SELECT id, status, hidden, result_json, uploader_hash, "
        "substr(created_at, 1, 10) AS day FROM jobs "
        "WHERE counted = 0 AND created_at < datetime('now', ?)",
        (cutoff_modifier,),
    ).fetchall()
    for row in rows:
        existing = conn.execute(
            "SELECT uploads, solved, failed, hidden, reasons_json "
            "FROM daily_stats WHERE day = ?", (row["day"],)).fetchone()
        if existing:
            uploads, solved, failed, hidden = (
                existing["uploads"], existing["solved"],
                existing["failed"], existing["hidden"])
            reasons = json.loads(existing["reasons_json"] or "{}")
        else:
            uploads = solved = failed = hidden = 0
            reasons = {}
        uploads += 1
        if row["status"] == "done":
            solved += 1
        elif row["status"] == "failed":
            failed += 1
            reason = failure_reason(row["result_json"])
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        if row["hidden"]:
            hidden += 1
        conn.execute(
            "INSERT INTO daily_stats "
            "(day, uploads, solved, failed, hidden, reasons_json) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET uploads = excluded.uploads, "
            "solved = excluded.solved, failed = excluded.failed, "
            "hidden = excluded.hidden, reasons_json = excluded.reasons_json",
            (row["day"], uploads, solved, failed, hidden, json.dumps(reasons)))
        if row["uploader_hash"]:
            conn.execute(
                "INSERT OR IGNORE INTO daily_uploaders (day, uploader_hash) "
                "VALUES (?, ?)", (row["day"], row["uploader_hash"]))
        conn.execute("UPDATE jobs SET counted = 1 WHERE id = ?", (row["id"],))
    return len(rows)


def history(conn, days=None):
    """The daily history, newest first: one dict per day with the counts
    and the number of distinct uploaders. `days` caps how many."""
    sql = ("SELECT s.day, s.uploads, s.solved, s.failed, s.hidden, "
           "s.reasons_json, "
           "(SELECT COUNT(*) FROM daily_uploaders u WHERE u.day = s.day) "
           "AS uploaders FROM daily_stats s ORDER BY s.day DESC")
    args = ()
    if days:
        sql += " LIMIT ?"
        args = (days,)
    return [{"day": r["day"], "uploads": r["uploads"], "solved": r["solved"],
             "failed": r["failed"], "hidden": r["hidden"],
             "reasons": json.loads(r["reasons_json"] or "{}"),
             "uploaders": r["uploaders"]}
            for r in conn.execute(sql, args)]
