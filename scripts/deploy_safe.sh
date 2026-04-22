#!/usr/bin/env bash
set -euo pipefail

ROOT_DEV="/home/node/.openclaw/workspace/options-monitor"
ROOT_PROD="/home/node/.openclaw/workspace/options-monitor-prod"
CFG_US="${OM_CANONICAL_CONFIG_US:-}"
CFG_HK="${OM_CANONICAL_CONFIG_HK:-}"

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

verify_runtime_configs=false
if [[ -n "$CFG_US" || -n "$CFG_HK" ]]; then
  if [[ -z "$CFG_US" || -z "$CFG_HK" ]]; then
    echo "[deploy-safe] FAIL: OM_CANONICAL_CONFIG_US and OM_CANONICAL_CONFIG_HK must be set together" >&2
    exit 1
  fi
  verify_runtime_configs=true
fi

before_us=""
before_hk=""
if [[ "$verify_runtime_configs" == true ]]; then
  cfg_us="$CFG_US"
  cfg_hk="$CFG_HK"
  if [[ ! -f "$cfg_us" || ! -f "$cfg_hk" ]]; then
    echo "[deploy-safe] FAIL: canonical runtime config missing" >&2
    echo "[deploy-safe] expected: $cfg_us and $cfg_hk" >&2
    exit 1
  fi

  before_us="$(sha256_file "$cfg_us")"
  before_hk="$(sha256_file "$cfg_hk")"
else
  echo "[deploy-safe] SKIP: runtime config hash guard disabled; set OM_CANONICAL_CONFIG_US/HK to enable"
fi

echo "[deploy-safe] step: dry-run"
python3 "$ROOT_DEV/scripts/deploy_to_prod.py" --dry-run

echo "[deploy-safe] step: apply"
python3 "$ROOT_DEV/scripts/deploy_to_prod.py" --apply

if [[ "$verify_runtime_configs" == true ]]; then
  after_us="$(sha256_file "$cfg_us")"
  after_hk="$(sha256_file "$cfg_hk")"

  if [[ "$before_us" != "$after_us" || "$before_hk" != "$after_hk" ]]; then
    echo "[deploy-safe] FAIL: canonical runtime config changed" >&2
    echo "[deploy-safe] config.us.json $before_us -> $after_us" >&2
    echo "[deploy-safe] config.hk.json $before_hk -> $after_hk" >&2
    exit 1
  fi

  echo "[deploy-safe] OK: runtime configs unchanged"
fi
