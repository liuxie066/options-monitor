#!/usr/bin/env bash

# Shared Python runtime selection for repository-owned shell entrypoints.
# This file is sourced by callers and intentionally has no source-time side effects.

_om_python_runtime_error() {
  printf 'options-monitor: Python >= 3.12 is required; %s\n' "$*" >&2
}

_om_python_command_path() {
  local candidate="$1"
  local resolved=""

  if [[ "$candidate" == */* ]]; then
    local parent base
    parent="$(dirname "$candidate")"
    base="$(basename "$candidate")"
    if [[ -d "$parent" ]]; then
      resolved="$(cd "$parent" 2>/dev/null && pwd -P)/$base"
    else
      resolved="$candidate"
    fi
  else
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
  fi

  printf '%s\n' "${resolved:-$candidate}"
}

_om_python_real_path() {
  local candidate="$1"
  local resolved link_target
  local hops=0

  resolved="$(_om_python_command_path "$candidate")"
  while [[ -L "$resolved" && "$hops" -lt 40 ]]; do
    link_target="$(readlink "$resolved")"
    if [[ "$link_target" == /* ]]; then
      resolved="$link_target"
    else
      resolved="$(dirname "$resolved")/$link_target"
    fi
    hops=$((hops + 1))
  done

  if [[ -n "$resolved" ]]; then
    local parent base
    parent="$(dirname "$resolved")"
    base="$(basename "$resolved")"
    if [[ -d "$parent" ]]; then
      resolved="$(cd "$parent" 2>/dev/null && pwd -P)/$base"
    fi
  fi

  printf '%s\n' "${resolved:-$candidate}"
}

_om_validate_python_candidate() {
  local candidate="$1"
  local label="$2"
  local resolved observed status

  resolved="$(_om_python_command_path "$candidate")"
  if [[ -z "$candidate" ]]; then
    _om_python_runtime_error "candidate=${label}; executable=<empty>; observed=not-found. Install Python 3.12 or set OM_PYTHON to a compatible executable."
    return 1
  fi
  if ! command -v "$candidate" >/dev/null 2>&1 && [[ ! -x "$candidate" ]]; then
    _om_python_runtime_error "candidate=${label}; executable=${resolved}; observed=not-found. Install Python 3.12 or set OM_PYTHON to a compatible executable."
    return 1
  fi

  if observed="$("$candidate" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3])); raise SystemExit(0 if sys.version_info >= (3, 12) else 42)' 2>&1)"; then
    printf '%s\n' "$resolved"
    return 0
  else
    status=$?
  fi

  observed="${observed//$'\n'/; }"
  if [[ "$status" -eq 42 ]]; then
    _om_python_runtime_error "candidate=${label}; executable=${resolved}; observed=${observed:-unknown}. Recreate .venv with Python 3.12 or set OM_PYTHON to a compatible executable."
  else
    _om_python_runtime_error "candidate=${label}; executable=${resolved}; observed=${observed:-unusable}. Install Python 3.12 or set OM_PYTHON to a compatible executable."
  fi
  return 1
}

_om_path_is_within() {
  local candidate_path="$1"
  local target_path="$2"
  [[ "$candidate_path" == "$target_path" || "$candidate_path" == "$target_path"/* ]]
}

om_select_repo_python() {
  local repo_root="$1"
  local repo_venv="$repo_root/.venv"
  local repo_python="$repo_venv/bin/python"

  if [[ -n "${OM_PYTHON:-}" ]]; then
    _om_validate_python_candidate "$OM_PYTHON" "OM_PYTHON"
    return
  fi

  if [[ -e "$repo_venv" || -L "$repo_venv" ]]; then
    _om_validate_python_candidate "$repo_python" "repo .venv"
    return
  fi

  if [[ -n "${PYTHON:-}" ]]; then
    _om_validate_python_candidate "$PYTHON" "PYTHON"
    return
  fi

  if command -v python3.12 >/dev/null 2>&1; then
    _om_validate_python_candidate "python3.12" "PATH python3.12"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    _om_validate_python_candidate "python3" "PATH python3"
    return
  fi

  _om_python_runtime_error "no interpreter candidate found. Install Python 3.12 or set OM_PYTHON to a compatible executable."
  return 1
}

om_select_bootstrap_python() {
  local target_venv="$1"
  local target_parent target_base target_path candidate label candidate_path

  target_parent="$(dirname "$target_venv")"
  target_base="$(basename "$target_venv")"
  if [[ -d "$target_venv" ]]; then
    target_path="$(cd "$target_venv" 2>/dev/null && pwd -P)"
  else
    target_path="$(cd "$target_parent" 2>/dev/null && pwd -P)/$target_base"
  fi

  if [[ -n "${OM_PYTHON:-}" ]]; then
    candidate="$OM_PYTHON"
    label="OM_PYTHON"
  elif [[ -n "${PYTHON:-}" ]]; then
    candidate="$PYTHON"
    label="PYTHON"
  elif command -v python3.12 >/dev/null 2>&1; then
    candidate="python3.12"
    label="PATH python3.12"
  elif command -v python3 >/dev/null 2>&1; then
    candidate="python3"
    label="PATH python3"
  else
    _om_python_runtime_error "no bootstrap interpreter candidate found. Install Python 3.12 or set OM_PYTHON to a compatible executable."
    return 1
  fi

  candidate_path="$(_om_python_real_path "$candidate")"
  if _om_path_is_within "$candidate_path" "$target_path"; then
    _om_python_runtime_error "candidate=${label}; executable=${candidate_path}; target_venv=${target_path}. The bootstrap interpreter must be outside the venv it creates or updates."
    return 1
  fi

  _om_validate_python_candidate "$candidate" "$label"
}
