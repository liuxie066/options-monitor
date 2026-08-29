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
# shellcheck source=python_runtime.sh
source "${ROOT}/scripts/python_runtime.sh"
PYTHON_BIN="$(om_select_repo_python "${ROOT}")"
export OM_PYTHON="${PYTHON_BIN}"

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

probe_loopback_bind() {
  local output status
  status=0
  output="$("${PYTHON_BIN}" -c '
# OM_RELEASE_PREFLIGHT_LOOPBACK_PROBE
import errno
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", 0))
except PermissionError as exc:
    print(f"PermissionError: [Errno {exc.errno}] {exc.strerror}", file=sys.stderr)
    raise SystemExit(77 if exc.errno == errno.EPERM else 78) from exc
except OSError as exc:
    print(f"OSError: [Errno {exc.errno}] {exc.strerror}", file=sys.stderr)
    raise SystemExit(78) from exc
finally:
    sock.close()
' 2>&1)" || status=$?

  if [[ "${status}" -eq 0 ]]; then
    echo "[PREFLIGHT_OK] loopback bind available (127.0.0.1)"
    return
  fi

  if [[ "${status}" -eq 77 ]]; then
    echo "[PREFLIGHT_ERROR] loopback bind denied: 127.0.0.1 socket.bind() returned PermissionError: [Errno 1] Operation not permitted" >&2
    echo "[PREFLIGHT_HINT] HTTP/model-path tests require local loopback listeners. Rerun this unchanged preflight outside the sandbox or grant loopback bind permission; do not skip or xfail the tests." >&2
    echo "[PREFLIGHT_HINT] No release or remote upgrade has started." >&2
    exit 1
  fi

  echo "[PREFLIGHT_ERROR] loopback bind probe failed before tests: ${output:-unknown error}" >&2
  echo "[PREFLIGHT_HINT] Resolve this local listener error before release preflight; it is not classified as sandbox EPERM." >&2
  exit 1
}

echo "[PREFLIGHT] root=${ROOT}"
echo "[PREFLIGHT] python=$("${PYTHON_BIN}" --version 2>&1)"

NODE_BIN="$(command -v node || true)"
NPM_BIN="$(command -v npm || true)"
if [[ -z "${NODE_BIN}" ]]; then
  echo "[PREFLIGHT_ERROR] Node >=22.19.0 is required" >&2
  exit 1
fi
if [[ -z "${NPM_BIN}" ]]; then
  echo "[PREFLIGHT_ERROR] npm is required" >&2
  exit 1
fi
NODE_VERSION="$("${NODE_BIN}" --version 2>/dev/null || true)"
if [[ ! "${NODE_VERSION}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] ||
   (( 10#${BASH_REMATCH[1]:-0} < 22 )) ||
   (( 10#${BASH_REMATCH[1]:-0} == 22 && 10#${BASH_REMATCH[2]:-0} < 19 )); then
  echo "[PREFLIGHT_ERROR] Node >=22.19.0 is required; observed=${NODE_VERSION:-unknown}" >&2
  exit 1
fi
echo "[PREFLIGHT] node=${NODE_VERSION}"

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

if (( FULL == 1 || FOCUSED == 1 )); then
  probe_loopback_bind
fi

run_step "Pi runtime locked install" \
  "${NPM_BIN}" ci --omit=dev --ignore-scripts --prefix agent-runtime
run_step "Pi runtime smoke" \
  bash scripts/pi_runtime_smoke.sh --root "${ROOT}" --python "${PYTHON_BIN}"

VERSION="$(tr -d '\n' < "${ROOT}/VERSION")"
run_step "release metadata" \
  "${PYTHON_BIN}" scripts/release_check.py --tag "v${VERSION}" --require-current-taxonomy --require-delta-coverage

if [[ "${CHECK_DEPS}" -eq 1 ]]; then
  run_step "dependency graph check" "${PYTHON_BIN}" scripts/generate_dependency_graph.py --check
fi

if [[ "${FOCUSED}" -eq 1 && "${FULL}" -eq 0 ]]; then
  run_step "Pi runtime focused tests" \
    "${PYTHON_BIN}" -m pytest tests/test_pi_agent_process.py
  run_step "Copilot, Control, and operations focused tests" \
    "${PYTHON_BIN}" -m pytest \
      tests/test_copilot_phase1.py \
      tests/test_copilot_conversation_memory.py \
      tests/test_copilot_p1_eval.py \
      tests/test_inbound_control.py \
      tests/test_setup_check.py \
      tests/test_cli_operator_commands.py \
      tests/test_install_script.py \
      tests/e2e/test_service_deploy_e2e.py \
      tests/integration/test_service_deploy_integration.py \
      tests/unit/test_service_deploy_unit.py \
      tests/test_release_check.py \
      tests/test_release_test_plan.py \
      tests/copilot_eval/test_answer_quality.py
  run_step "agent/plugin focused tests" \
    "${PYTHON_BIN}" -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
  run_step "research domain focused tests" \
    "${PYTHON_BIN}" -m pytest \
      tests/test_research.py \
      tests/test_research_archive.py \
      tests/test_shadow_replay.py \
      tests/test_shadow_replay_candidate_impact.py \
      tests/test_strategy_lab_update.py \
      tests/test_strategy_lab_top1_architecture.py
  run_step "configuration focused tests" \
    "${PYTHON_BIN}" -m pytest \
      tests/test_config_yaml.py \
      tests/test_config_template_inheritance.py \
      tests/test_config_authoring_transaction.py \
      tests/test_runtime_config_identity.py
fi

if [[ "${FULL}" -eq 1 ]]; then
  run_step "full pytest" "${PYTHON_BIN}" -m pytest
else
  echo "[PREFLIGHT] full pytest skipped; pass --full for release-final validation"
fi

echo "[PREFLIGHT_OK] release preflight complete for ${VERSION}"
