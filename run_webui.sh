#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
HOST="${OM_WEBUI_HOST:-127.0.0.1}"
PORT="${OM_WEBUI_PORT:-8000}"

cd "$ROOT"

if [[ ! -x ".venv/bin/python" || ! -x ".venv/bin/pip" ]]; then
  echo "[run-webui] step: create venv"
  "$PY" -m venv .venv
fi

echo "[run-webui] step: install python deps"
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

if command -v npm >/dev/null 2>&1; then
  echo "[run-webui] step: build frontend"
  bash scripts/webui/build_frontend.sh
else
  echo "[run-webui] skip: npm not found, using checked-in static assets"
fi

echo "[run-webui] start: http://${HOST}:${PORT}"
exec ./.venv/bin/python -m uvicorn scripts.webui.server:app --host "${HOST}" --port "${PORT}"
