#!/usr/bin/env bash
# Run worker + web in one container: the Fly deploy is a single machine so
# both processes share the SQLite volume (#8). Either process exiting — or
# a platform SIGTERM/SIGINT — tears down the other so the machine restarts
# whole. No `set -e`: wait -n's nonzero status must not skip the cleanup.
set -uo pipefail

# -u: unbuffered, or worker prints sit in a block buffer until shutdown.
python -u -m app.worker &
WORKER=$!

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
WEB=$!

shutdown() {
  trap - TERM INT
  kill "$WORKER" "$WEB" 2>/dev/null || true
}
trap shutdown TERM INT

wait -n "$WORKER" "$WEB"
rc=$?
echo "start-all: shutting down (first exit/signal status: $rc)" >&2
shutdown
wait "$WORKER" "$WEB" 2>/dev/null
exit "$rc"
