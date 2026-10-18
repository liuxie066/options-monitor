#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/python_runtime.sh"
VPY="$(om_select_repo_python "$ROOT")"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$VPY" -m src.application.positions.auto_close_schedule "$@"
