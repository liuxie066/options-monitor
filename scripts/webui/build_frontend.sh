#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/frontend"

if ! command -v npm >/dev/null 2>&1; then
  echo "[ERROR] npm not found"
  exit 1
fi

# Some environments default to production installs; force dev deps (vite/plugin-react)
npm install --include=dev

# Use local binary via npm exec
npm exec -- vite build

echo "[OK] built to ../static"
