#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VPY="${PYTHON:-python3}"

cd "$ROOT"

echo "[install-agent] step: create venv"
"$VPY" -m venv .venv

echo "[install-agent] step: install python deps"
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "[install-agent] step: verify public launcher"
./om-agent spec >/dev/null

echo "[install-agent] step: prepare local secrets directory"
mkdir -p secrets

echo "[install-agent] OK"
echo "[install-agent] next:"
echo "  1) start OpenD and confirm it is logged in"
echo "  2) run ./run_webui.sh"
echo "  3) finish first-time initialization in the local WebUI"
