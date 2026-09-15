"""Visitor feedback as GitHub issues (#137).

There was no way for a visitor to say something went wrong, or suggest
something, without leaving the site and finding the repo. A small button
opens a dialog, and what they write is filed as an issue here, with the
context we would otherwise have to ask for: which job, what state it was
in, what the solve found, which browser.

The endpoint is unauthenticated and writes to a public repository, so it
is defensive: a token is required or the endpoint does not exist (404,
like the admin endpoints); the request body is bounded before it is
parsed and the text is capped after; a per-address cooldown and a global
daily cap bound the worst case, the cap kept in the database so the
machine's auto-restarts don't reset it and reserved under a lock so a
burst can't overshoot it; and every @ in the visitor's words, title
included, is neutralised so a report can never page a GitHub user.
GITHUB_TOKEN needs only issues write on this repository; it is never
logged, not even on failure.
"""

import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPO", "cwage/asterism").strip()
LABEL = os.environ.get("FEEDBACK_LABEL", "bug-report")
MAX_BODY_BYTES = 16 * 1024      # the whole request, before anything parses it
MAX_DESCRIPTION_CHARS = 2000
MAX_CONTEXT_CHARS = 5000
MAX_TITLE_CHARS = 80
COOLDOWN_SECONDS = 60           # per client address
PER_DAY = int(os.environ.get("FEEDBACK_PER_DAY", "20"))  # across everyone
TIMEOUT_SECONDS = 10.0
META_KEY = "feedback:filed"     # JSON list of {"id", "at"} for the last day

_cap_lock = threading.Lock()


def enabled():
    return bool(TOKEN and REPO)


def _load(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
    try:
        entries = json.loads(row["value"]) if row else []
    except (TypeError, ValueError):
        entries = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return [e for e in entries if isinstance(e, dict) and e.get("at", "") > cutoff]


def _save(conn, entries):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (META_KEY, json.dumps(entries)))


def reserve(conn):
    """Take one of today's slots, or None when they are spent. The slot
    is written before the issue is posted and released if that fails, so
    the cap counts filings, a burst can't see the same free slot twice
    (the lock), and a restart doesn't hand out a fresh day (the row)."""
    with _cap_lock:
        entries = _load(conn)
        if len(entries) >= PER_DAY:
            return None
        slot = {"id": uuid.uuid4().hex, "at": datetime.now(timezone.utc).isoformat()}
        _save(conn, entries + [slot])
        return slot["id"]


def release(conn, slot):
    with _cap_lock:
        _save(conn, [e for e in _load(conn) if e.get("id") != slot])


def filed_today(conn):
    return len(_load(conn))


def neutralise(text):
    """The visitor's words, unable to page anyone or break out of the
    fence they are shown in: every @ becomes a full-width one, and every
    run of backticks loses its power (str.replace is global)."""
    return text.replace("@", "＠").replace("```", "'''")


def title_for(description):
    first = " ".join(description.strip().splitlines()[0].split()) if description.strip() else "Feedback"
    if len(first) > MAX_TITLE_CHARS:
        first = first[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return "Feedback: " + neutralise(first).replace("`", "'")


def body_for(description, client_context, server_context):
    """Three fenced sections: what the visitor wrote, what their browser
    knew, what the server knew. Fenced so nothing in them renders as
    markdown, links, or mentions."""
    client = json.dumps(client_context, indent=1, sort_keys=True, ensure_ascii=False)
    if len(client) > MAX_CONTEXT_CHARS:
        client = client[:MAX_CONTEXT_CHARS] + "\n… (truncated)"
    server = json.dumps(server_context, indent=1, sort_keys=True, ensure_ascii=False)
    return (
        "A visitor filed this from the site's feedback dialog.\n\n"
        "### What they wrote\n\n```text\n" + neutralise(description) + "\n```\n\n"
        "### From their browser\n\n```json\n" + neutralise(client) + "\n```\n\n"
        "### From the server\n\n```json\n" + neutralise(server) + "\n```\n"
    )


def _post(payload):
    """POST the issue; returns the decoded response on 201, raises
    otherwise. Separate so tests can stand in for GitHub."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/issues",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "asterism-feedback"},
        method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 201:
            raise RuntimeError(f"unexpected status {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _redact(text):
    return text.replace(TOKEN, "<token>") if TOKEN else text


def file_report(description, client_context, user_agent=None, job=None):
    """File the issue. Returns {"number", "url"} or None when GitHub did
    not take it (the reason goes to the log, with the token redacted).
    The daily-cap slot is the caller's: reserve() before, release() on
    None."""
    description = description.strip()[:MAX_DESCRIPTION_CHARS]
    server_context = {
        "filed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "user_agent": (user_agent or "")[:300],
    }
    if job:
        server_context["job"] = job
    payload = {"title": title_for(description),
               "body": body_for(description, client_context or {}, server_context),
               "labels": [LABEL]}
    try:
        issue = _post(payload)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(500).decode("utf-8", "replace")
        except Exception:
            pass
        print(f"feedback: GitHub refused the issue (HTTP {e.code}) {_redact(detail)}")
        return None
    except Exception as e:
        print(f"feedback: filing failed ({type(e).__name__}: {_redact(str(e))})")
        return None
    return {"number": issue.get("number"), "url": issue.get("html_url")}
