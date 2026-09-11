#!/usr/bin/env bash
# Runs the API and the Vite dev server together. Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
export TOKOP_MODE="${TOKOP_MODE:-replay}"
echo "tokop dev: mode=$TOKOP_MODE  api=http://127.0.0.1:8000  web=http://127.0.0.1:5173"
engine/.venv/bin/python -m uvicorn tokop.api.app:app --host 127.0.0.1 --port 8000 --reload \
  --app-dir engine &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT INT TERM
(cd web && pnpm dev)
