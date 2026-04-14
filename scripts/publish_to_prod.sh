#!/usr/bin/env bash
set -Eeuo pipefail

# Publish dev repo changes into the local prod checkout.
#
# Design goals:
# - Avoid editing prod directly (no drift): always publish from dev.
# - Copy ONLY tracked files from dev (git ls-files), so local prod config.us.json/config.hk.json/output/.venv are untouched.
# - Create an auditable publish commit on a timestamped branch in prod, then merge into a target branch.
#
# Default dirs match this OpenClaw workspace layout.
DEV_DIR="/home/node/.openclaw/workspace/options-monitor"
PROD_DIR="/home/node/.openclaw/workspace/options-monitor-prod"
TARGET_BRANCH="hotfix/scheduled-speed-quiet-20260327"
# Default behavior: create a publish commit on a publish/* branch, but do NOT merge into target.
# Use --apply to merge (after manual confirmation).
DO_MERGE=0
APPLY=0
ALLOW_DIRTY=0

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [--dev <dir>] [--prod <dir>] [--target-branch <name>] [--apply] [--no-merge] [--allow-dirty]

Examples:
  # Create publish commit (does NOT affect production yet)
  $(basename "$0")

  # Apply to production (merge publish branch into target branch)
  $(basename "$0") --apply

  # Publish but do not merge into target branch (explicit)
  $(basename "$0") --no-merge

Notes:
- This script performs ONLY local git commits/merges; it does NOT push to GitHub.
- It copies files from dev using: git ls-files (tracked only).
  So ignored local files like config.us.json/config.hk.json/output/.venv are NOT overwritten.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV_DIR="$2"; shift 2;;
    --prod) PROD_DIR="$2"; shift 2;;
    --target-branch) TARGET_BRANCH="$2"; shift 2;;
    --apply) APPLY=1; DO_MERGE=1; shift 1;;
    --no-merge) DO_MERGE=0; shift 1;;
    --allow-dirty) ALLOW_DIRTY=1; shift 1;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 2;;
  esac
done

need_git_repo() {
  local dir="$1"
  if [[ ! -d "$dir/.git" ]]; then
    echo "[ERR] not a git repo: $dir" >&2
    exit 1
  fi
}

ensure_clean() {
  local dir="$1" label="$2"
  if [[ $ALLOW_DIRTY -eq 1 ]]; then
    return 0
  fi
  local st
  st=$(cd "$dir" && git status --porcelain=v1)
  if [[ -n "$st" ]]; then
    echo "[ERR] $label repo has uncommitted changes. Commit/stash them or pass --allow-dirty." >&2
    echo "$st" >&2
    exit 1
  fi
}

need_git_repo "$DEV_DIR"
need_git_repo "$PROD_DIR"
ensure_clean "$DEV_DIR" "DEV"
ensure_clean "$PROD_DIR" "PROD"

DEV_HEAD=$(cd "$DEV_DIR" && git rev-parse --short HEAD)
DEV_BRANCH=$(cd "$DEV_DIR" && git branch --show-current)
PROD_HEAD_BEFORE=$(cd "$PROD_DIR" && git rev-parse --short HEAD)

OBS_STATUS="failed"
OBS_FAILURE_REASON=""
OBS_MERGED_TO_TARGET=0
OBS_PUBLISH_BRANCH=""

record_observability() {
  local rc="$?"
  local py_bin prod_after status reason merged
  status="${OBS_STATUS}"
  reason="${OBS_FAILURE_REASON}"
  merged="${OBS_MERGED_TO_TARGET}"

  if [[ "$rc" -ne 0 ]]; then
    status="failed"
    if [[ -z "$reason" ]]; then
      reason="exit_code=${rc}"
    fi
  fi

  if [[ -d "$PROD_DIR/.git" ]]; then
    prod_after=$(cd "$PROD_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  else
    prod_after="unknown"
  fi

  py_bin="$DEV_DIR/.venv/bin/python"
  if [[ ! -x "$py_bin" ]]; then
    py_bin="$(command -v python3 || command -v python)"
  fi

  OBS_DEV_DIR="$DEV_DIR" \
  OBS_STATUS="$status" \
  OBS_REASON="$reason" \
  OBS_DEV_HEAD="${DEV_HEAD:-unknown}" \
  OBS_PROD_BEFORE="${PROD_HEAD_BEFORE:-unknown}" \
  OBS_PROD_AFTER="$prod_after" \
  OBS_TARGET_BRANCH="${TARGET_BRANCH:-}" \
  OBS_MERGED="$merged" \
  OBS_APPLY="${APPLY:-0}" \
  OBS_PUBLISH_BRANCH="${OBS_PUBLISH_BRANCH:-}" \
  "$py_bin" - <<'PY' || true
import os
import sys

dev_dir = os.environ.get("OBS_DEV_DIR", "")
if dev_dir:
    sys.path.insert(0, dev_dir)

from scripts.deploy_observability import append_event, build_summary, make_machine_json, utc_now

status = os.environ.get("OBS_STATUS", "failed")
reason = os.environ.get("OBS_REASON", "")
event = {
    "timestamp_utc": utc_now(),
    "operation": "publish",
    "status": status,
    "dev_commit": os.environ.get("OBS_DEV_HEAD", "unknown"),
    "prod_commit_before": os.environ.get("OBS_PROD_BEFORE", "unknown"),
    "prod_commit_after": os.environ.get("OBS_PROD_AFTER", "unknown"),
    "target_branch": os.environ.get("OBS_TARGET_BRANCH", ""),
    "merged_to_target": os.environ.get("OBS_MERGED", "0") == "1",
    "applied": os.environ.get("OBS_APPLY", "0") == "1",
    "publish_branch": os.environ.get("OBS_PUBLISH_BRANCH", ""),
}
if status == "failed":
    event["failure_reason"] = reason or "unknown"

recorded = append_event(event)
print(f"[publish][json] {make_machine_json(recorded)}")
print(build_summary(recorded))
if bool(recorded.get("should_alert")):
    print("[publish][alert] state-changed-or-new-failure")
PY
}

trap 'OBS_FAILURE_REASON="line:${LINENO} cmd:${BASH_COMMAND}"' ERR
trap 'record_observability' EXIT

STAMP=$(date -u +%Y%m%d-%H%M%S)
PUBLISH_BRANCH="publish/from-dev-${STAMP}"
OBS_PUBLISH_BRANCH="$PUBLISH_BRANCH"

echo "[INFO] DEV:  $DEV_DIR ($DEV_BRANCH @ $DEV_HEAD)"
echo "[INFO] PROD: $PROD_DIR (target branch: $TARGET_BRANCH)"
echo "[INFO] publish branch: $PUBLISH_BRANCH"

# Create publish branch in prod
(
  cd "$PROD_DIR"
  git checkout -b "$PUBLISH_BRANCH" >/dev/null
)

# Copy tracked files from dev into prod
TRACKED=$(cd "$DEV_DIR" && git ls-files)
while IFS= read -r f; do
  case "$f" in
    output*|output_accounts*|secrets*|.venv*|.tmp_* )
      continue;;
  esac
  src="$DEV_DIR/$f"
  dst="$PROD_DIR/$f"
  mkdir -p "$(dirname "$dst")"
  # Preserve mode/timestamps where possible
  cp -a "$src" "$dst"
done <<< "$TRACKED"

# Compile quick sanity check (scripts only)
"$PROD_DIR/.venv/bin/python" -m py_compile "$PROD_DIR"/scripts/*.py

# Commit on publish branch
(
  cd "$PROD_DIR"
  git add -A
  if git diff --cached --quiet; then
    echo "[INFO] No changes to publish (prod already matches dev tracked files)."
  else
    git commit -m "publish: sync dev(${DEV_BRANCH}@${DEV_HEAD}) -> prod" >/dev/null
    echo "[OK] publish commit created: $(git rev-parse --short HEAD)"
  fi
)

if [[ $DO_MERGE -eq 1 ]]; then
  if [[ $APPLY -ne 1 ]]; then
    echo "[ERR] Refusing to merge without explicit --apply" >&2
    exit 1
  fi
  (
    cd "$PROD_DIR"
    git checkout "$TARGET_BRANCH" >/dev/null
    git merge --no-ff "$PUBLISH_BRANCH" -m "merge: publish ${PUBLISH_BRANCH}" >/dev/null
    echo "[OK] merged into $TARGET_BRANCH: $(git rev-parse --short HEAD)"
  )
  OBS_MERGED_TO_TARGET=1
else
  echo "[INFO] --no-merge set: leaving changes on $PUBLISH_BRANCH"
fi

OBS_STATUS="success"
echo "[DONE] publish complete"
