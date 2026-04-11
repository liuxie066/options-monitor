#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

mkdir -p .githooks
chmod +x .githooks/pre-commit .githooks/commit-msg

git config core.hooksPath .githooks

echo "[guardrails] git hooks enabled at .githooks"
echo "[guardrails] active hooks: pre-commit, commit-msg"
