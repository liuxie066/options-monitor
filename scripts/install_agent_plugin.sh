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

echo "[install-agent] OK"
echo "[install-agent] next:"
echo "  1) copy configs/examples/config.example.us.json to config.us.json or set OM_CONFIG_DIR"
echo "  2) configure portfolio.pm_config or OM_PM_CONFIG if you need holdings-backed tools"
echo "  3) run ./om-agent spec"
