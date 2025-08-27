#!/usr/bin/env bash
set -euo pipefail

# IDs used to chown generated files back to the host user
OUT_UID="${NIFTYTTS_UID:-99}"
OUT_GID="${NIFTYTTS_GID:-100}"

# Which watcher to run? default = edge
WATCHER="${BACKEND:-edge}"

echo "[entrypoint] Starting NiftyTTS with BACKEND='${WATCHER}'"

# Start web app
python -m uvicorn app:app --host 0.0.0.0 --port 7230 &
WEB_PID=$!

# Map BACKEND -> watcher script
case "$WATCHER" in
  edge|EDGE)
    WATCH_CMD=(python watchers/tts_watch_edge.py)
    ;;
  piper|PIPER)
    WATCH_CMD=(python watchers/tts_watch_piper.py)
    ;;
  local|sapi|LOCAL)
    WATCH_CMD=(python watchers/tts_watch_pyttsx.py)
    ;;
  *)
    echo "[entrypoint] Unknown BACKEND '${WATCHER}', falling back to 'edge'"
    WATCH_CMD=(python watchers/tts_watch_edge.py)
    ;;
esac

# Start watcher
"${WATCH_CMD[@]}" &
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
