#!/bin/sh
set -e
mkdir -p "${DATA_DIR:-/data}" 2>/dev/null || true
if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser "${DATA_DIR:-/data}" 2>/dev/null || true
    exec setpriv --reuid=1000 --regid=1000 --clear-groups \
        env PORT="${PORT:-8000}" \
        python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
