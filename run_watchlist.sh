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

echo "[RUN] watchlist pipeline (${OPTIONS_MONITOR_CONFIG:-config.us.json})"
exec .venv/bin/python scripts/run_pipeline.py --config "${OPTIONS_MONITOR_CONFIG:-config.us.json}"
