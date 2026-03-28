#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Ensure venv exists
if [[ ! -x ".venv/bin/python" ]]; then
  echo "[BOOTSTRAP] creating .venv"
  python3 -m venv .venv
fi

# Ensure deps installed (best-effort idempotent)
if ! .venv/bin/python - <<'PY' >/dev/null 2>&1
import pandas, yfinance, yaml, tabulate
PY
then
  echo "[BOOTSTRAP] installing deps from requirements.txt"
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi

CFG_DEFAULT="config.local.us.json"
CFG="${OPTIONS_MONITOR_CONFIG:-$CFG_DEFAULT}"

if [[ ! -f "$CFG" ]]; then
  echo "[ERR] config file not found: $CFG"
  echo "      Create one from example:"
  echo "      cp config.example.us.json $CFG_DEFAULT"
  echo "      # or for HK: cp config.example.hk.json config.local.hk.json"
  exit 2
fi

echo "[RUN] watchlist pipeline ($CFG)"
exec .venv/bin/python scripts/run_pipeline.py --config "$CFG"
