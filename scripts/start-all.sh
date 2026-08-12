#!/usr/bin/env bash
# Run worker + web in one container: the Fly deploy is a single machine so
# both processes share the SQLite volume (#8). If either side dies, exit so
# the platform restarts the machine whole.
set -euo pipefail

python -m app.worker &
WORKER=$!

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
WEB=$!

wait -n "$WORKER" "$WEB"
echo "start-all: a process exited; shutting down" >&2
kill "$WORKER" "$WEB" 2>/dev/null || true
exit 1
