"""Visitor feedback as GitHub issues (#137).

There was no way for a visitor to say something went wrong, or suggest
something, without leaving the site and finding the repo. A small button
opens a dialog, and what they write is filed as an issue here, with the
context we would otherwise have to ask for: which job, what state it was
in, what the solve found, which browser.

The endpoint is unauthenticated and writes to a public repository, so it
is defensive: a token is required or it does not exist (503); the text
is capped; a per-address cooldown and a global daily cap bound the
worst case; and every @ in the visitor's words is neutralised so a
report can never page a GitHub user. GITHUB_TOKEN needs only issues
write on this repository; it is never logged, not even on failure.
"""

import json
import os
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPO", "cwage/asterism").strip()
LABEL = os.environ.get("FEEDBACK_LABEL", "bug-report")
MAX_DESCRIPTION_CHARS = 2000
MAX_CONTEXT_CHARS = 5000
MAX_TITLE_CHARS = 80
COOLDOWN_SECONDS = 60           # per client address
PER_DAY = int(os.environ.get("FEEDBACK_PER_DAY", "20"))  # across everyone
TIMEOUT_SECONDS = 10.0

_filed = deque()  # monotonic times of reports filed today, for the daily cap


def enabled():
    return bool(TOKEN and REPO)


def daily_cap_reached(now=None):
    """Whether today's global allowance is spent. Counts filings, not
    attempts: the point is bounding what lands in the repo."""
    now = time.monotonic() if now is None else now
    while _filed and _filed[0] <= now - 86400:
        _filed.popleft()
    return len(_filed) >= PER_DAY


def _record_filed(now=None):
    _filed.append(time.monotonic() if now is None else now)


def neutralise(text):
    """The visitor's words, unable to page anyone or break out of the
    fence they are shown in: every @ becomes a full-width one, and a
    run of backticks loses its power."""
    return text.replace("@", "＠").replace("```", "'''")


def title_for(description):
    first = " ".join(description.strip().splitlines()[0].split()) if description.strip() else "Feedback"
    if len(first) > MAX_TITLE_CHARS:
        first = first[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return f"Feedback: {first}"


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


def file_report(description, client_context, user_agent=None, job=None):
    """File the issue. Returns {"number", "url"} or None when GitHub did
    not take it (the reason goes to the log, without the token)."""
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
        print(f"feedback: GitHub refused the issue (HTTP {e.code})")
        return None
    except Exception as e:
        print(f"feedback: filing failed ({type(e).__name__})")
        return None
    _record_filed()
    return {"number": issue.get("number"), "url": issue.get("html_url")}
