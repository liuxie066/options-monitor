#!/usr/bin/env bash
set -euo pipefail

ROOT_DEV="/home/node/.openclaw/workspace/options-monitor"
ROOT_PROD="/home/node/.openclaw/workspace/options-monitor-prod"

if [[ ! -d "$ROOT_PROD" ]]; then
  echo "[deploy-safe] FAIL: missing prod repo at $ROOT_PROD" >&2
  exit 1
fi

sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  else
    shasum -a 256 "$file" | awk '{print $1}'
  fi
}

cfg_us="$ROOT_PROD/config.us.json"
cfg_hk="$ROOT_PROD/config.hk.json"

if [[ ! -f "$cfg_us" || ! -f "$cfg_hk" ]]; then
  echo "[deploy-safe] FAIL: prod runtime config missing" >&2
  echo "[deploy-safe] expected: $cfg_us and $cfg_hk" >&2
  exit 1
fi

before_us="$(sha256_file "$cfg_us")"
before_hk="$(sha256_file "$cfg_hk")"

echo "[deploy-safe] step: dry-run"
python3 "$ROOT_DEV/scripts/deploy_to_prod.py" --dry-run

echo "[deploy-safe] step: apply"
python3 "$ROOT_DEV/scripts/deploy_to_prod.py" --apply

after_us="$(sha256_file "$cfg_us")"
after_hk="$(sha256_file "$cfg_hk")"

if [[ "$before_us" != "$after_us" || "$before_hk" != "$after_hk" ]]; then
  echo "[deploy-safe] FAIL: prod runtime config changed" >&2
  echo "[deploy-safe] config.us.json $before_us -> $after_us" >&2
  echo "[deploy-safe] config.hk.json $before_hk -> $after_hk" >&2
  exit 1
fi

echo "[deploy-safe] OK: runtime configs unchanged"
