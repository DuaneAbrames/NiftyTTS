#!/usr/bin/env bash
set -euo pipefail

# IDs used to chown generated files back to the host user
OUT_UID="${NIFTYTTS_UID:-99}"
OUT_GID="${NIFTYTTS_GID:-100}"

echo "[entrypoint] Starting NiftyTTS"

# Start web app
python -m uvicorn app.app:app --host 0.0.0.0 --port 7230 &
WEB_PID=$!

# Start dispatcher watcher (per-job backend selection supported)
python -m app.watchers.dispatcher_watch &
WATCH_PID=$!

# Wait on either to exit; then stop both
wait -n "$WEB_PID" "$WATCH_PID"
EXIT_CODE=$?
kill "$WEB_PID" "$WATCH_PID" 2>/dev/null || true
wait || true

# Chown output files to requested user/group
if [ -d jobs ]; then
  echo "[entrypoint] Chowning outputs to ${OUT_UID}:${OUT_GID}"
  chown -R "${OUT_UID}:${OUT_GID}" jobs || true
fi

exit "$EXIT_CODE"
