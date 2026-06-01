#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/release_preflight.sh [options]

Local release preflight. It does not commit, push, tag, or publish.

Options:
  --full             Run the full pytest suite after fast checks.
  --require-clean    Fail when the git worktree has uncommitted changes.
  --allow-dirty      Report dirty worktree state but do not fail. Default.
  --skip-focused     Skip focused agent/plugin tests.
  --skip-deps        Skip dependency graph freshness check.
  -h, --help         Show this help.
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_has_pytest() {
  "$1" -c 'import pytest' >/dev/null 2>&1
}

PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]] && python_has_pytest "${ROOT}/.venv/bin/python"; then
    PYTHON_BIN="${ROOT}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

FULL=0
REQUIRE_CLEAN=0
FOCUSED=1
CHECK_DEPS=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      FULL=1
      ;;
    --require-clean)
      REQUIRE_CLEAN=1
      ;;
    --allow-dirty)
      REQUIRE_CLEAN=0
      ;;
    --skip-focused)
      FOCUSED=0
      ;;
    --skip-deps)
      CHECK_DEPS=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[PREFLIGHT_ERROR] unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

run_step() {
  local label="$1"
  shift
  local start end elapsed
  start="$(date +%s)"
  echo "[PREFLIGHT] ${label}"
  (
    cd "${ROOT}"
    "$@"
  )
  end="$(date +%s)"
  elapsed=$((end - start))
  echo "[PREFLIGHT_OK] ${label} (${elapsed}s)"
}

echo "[PREFLIGHT] root=${ROOT}"
echo "[PREFLIGHT] python=$("${PYTHON_BIN}" --version 2>&1)"

status="$(git -C "${ROOT}" status --short)"
if [[ -n "${status}" ]]; then
  echo "[PREFLIGHT_WARN] git worktree has uncommitted changes:"
  echo "${status}"
  if [[ "${REQUIRE_CLEAN}" -eq 1 ]]; then
    echo "[PREFLIGHT_ERROR] dirty worktree; rerun with --allow-dirty to inspect only" >&2
    exit 1
  fi
else
  echo "[PREFLIGHT_OK] git worktree clean"
fi

VERSION="$(tr -d '\n' < "${ROOT}/VERSION")"
run_step "release metadata" "${PYTHON_BIN}" scripts/release_check.py --tag "v${VERSION}"

if [[ "${CHECK_DEPS}" -eq 1 ]]; then
  run_step "dependency graph check" "${PYTHON_BIN}" scripts/generate_dependency_graph.py --check
fi

if [[ "${FOCUSED}" -eq 1 ]]; then
  run_step "agent/plugin focused tests" \
    "${PYTHON_BIN}" -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
fi

if [[ "${FULL}" -eq 1 ]]; then
  run_step "full pytest" "${PYTHON_BIN}" -m pytest
else
  echo "[PREFLIGHT] full pytest skipped; pass --full for release-final validation"
fi

echo "[PREFLIGHT_OK] release preflight complete for ${VERSION}"
