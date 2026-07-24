#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=python_runtime.sh
source "$ROOT/scripts/python_runtime.sh"
VPY="$(om_select_bootstrap_python "$ROOT/.venv")"

cd "$ROOT"

echo "[install-agent] step: create venv"
"$VPY" -m venv .venv

echo "[install-agent] step: install python deps"
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt

echo "[install-agent] step: verify public launcher"
./om-agent spec >/dev/null

echo "[install-agent] step: prepare local secrets directory"
mkdir -p secrets

echo "[install-agent] OK"
echo "[install-agent] next:"
echo "  1) start OpenD and confirm it is logged in"
echo "  2) initialize config with ./om config init --output config.yaml --runtime-output-dir . --futu-acc-id <id>"
echo "  3) optional: render services with ./om service render and use the generated service.profile.json for runtime_status"
