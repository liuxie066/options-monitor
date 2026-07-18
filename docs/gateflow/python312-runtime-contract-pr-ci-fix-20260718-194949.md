# Gateflow Fix Artifact — Python 3.12 Runtime Contract PR CI

## Gate

- Work unit: `python312-runtime-contract`
- Gate: draft PR CI fix
- Branch: `codex/python312-runtime-contract`
- PR: `#81`
- Failing head: `31973abeaf99de505a507fb60fff351696a8c375`
- Artifact path: `docs/gateflow/python312-runtime-contract-pr-ci-fix-20260718-194949.md`
- Completion status: complete; ready for PR re-review and push

## Failure Evidence

- `Agent Plugin` run `29643066367`, job `88076649614`: `scripts/install_agent_plugin.sh` created `.venv` and installed dependencies, but `./om-agent spec` ran the underlying system Python and failed with `ModuleNotFoundError: No module named 'yaml'`.
- `Guardrails` run `29643066345`, job `88076649575`: lint and guardrail checks passed; `tests/run_smoke.py` failed because its `om-agent spec` subprocess returned non-zero for the same reason.
- CodeQL jobs passed.

## Root Cause

The preceding PR-review fix changed `_om_python_command_path` to resolve the executable file's final symlink. A normal virtualenv commonly implements `.venv/bin/python` as a symlink to the base interpreter. The repository selector therefore returned the base interpreter path instead of the venv entrypoint, losing venv dependency context.

## Fix

- Restored `_om_python_command_path` as the semantic execution-path resolver: it canonicalizes parent directories but preserves the final executable entrypoint, including `.venv/bin/python` symlinks.
- Added `_om_python_real_path` solely for bootstrap physical-containment checks.
- Kept the external-alias-to-target rejection by using `_om_python_real_path` only in `om_select_bootstrap_python`.
- Added `test_repo_selector_preserves_venv_python_symlink_entrypoint`.

## Validation

- Runtime-contract focused tests: `11 passed`.
- `./om-agent spec` passed with the clean worktree temporarily linked to the repository runtime-dependency venv.
- Full pytest: `2680 passed, 10 skipped in 95.57s`.
- Temporary `.venv` link removed after validation.
- `ruff check .`: pass.
- Python 3.12 compileall: pass.
- Dependency graph check: 468 production modules, zero cycles.
- Bash parse and `git diff --check`: pass.

## Residual Risks

- Direct generic-shebang execution, exact Python patch-version drift, and historical command snapshots retain their previously accepted classifications.
- No unclassified residual risk or blocking open question remains.
