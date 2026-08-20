#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: pi_runtime_smoke.sh --root ROOT --python PYTHON\n'
}

die() {
  printf 'pi_runtime_smoke.sh: %s\n' "$*" >&2
  exit 1
}

ROOT=""
PYTHON_BIN=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root)
      [ "$#" -ge 2 ] || die "--root requires a value"
      ROOT="$2"
      shift 2
      ;;
    --python)
      [ "$#" -ge 2 ] || die "--python requires a value"
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$ROOT" ] || die "--root is required"
[ -n "$PYTHON_BIN" ] || die "--python is required"
[ -d "$ROOT" ] || die "root directory not found: $ROOT"
ROOT="$(cd "$ROOT" && pwd -P)"
cd "$ROOT"

if [[ "$PYTHON_BIN" != */* ]]; then
  PYTHON_BIN="$(command -v "$PYTHON_BIN" 2>/dev/null || true)"
fi
[ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ] || die "python executable not found: ${PYTHON_BIN:-<empty>}"
PYTHON_BIN="$(cd "$(dirname "$PYTHON_BIN")" && pwd -P)/$(basename "$PYTHON_BIN")"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
export OM_RUNTIME_ROOT="$tmp_dir/runtime"
export OM_ENV_FILE="$tmp_dir/missing.env"
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME \
  OM_PI_SESSION_DB OM_PI_MODEL_API_KEY OM_LLM_API_KEY OPENAI_API_KEY \
  DEEPSEEK_API_KEY MOONSHOT_API_KEY KIMI_API_KEY ANTHROPIC_API_KEY \
  GEMINI_API_KEY GOOGLE_API_KEY

(
  cd "$ROOT/agent-runtime"
  node --input-type=module --eval \
    'await Promise.all(["@earendil-works/pi-agent-core", "@earendil-works/pi-ai", "@earendil-works/pi-session-backend-sqlite-node"].map((name) => import(name)))'
)

response="$(
  OM_PYTHON="$PYTHON_BIN" "$ROOT/om" copilot eval \
    --fixture current_option_exposure_model_ready \
    --model-turn-json '{"text":"Pi runtime ready."}'
)"
printf '%s' "$response" | "$PYTHON_BIN" -c \
  'import json, sys; payload = json.load(sys.stdin); assert payload.get("status") == "answered"; assert payload.get("user_response") == "Pi runtime ready."'

if find "$tmp_dir" -name pi_sessions.sqlite3 -print -quit | grep -q .; then
  die "deterministic smoke created a Pi Session database"
fi

printf '[pi-runtime] smoke passed\n'
