# Gateflow Fix Artifact — Python 3.12 Runtime Contract PR Review

## Gate

- Work unit: `python312-runtime-contract`
- Gate: PR review fix
- Branch: `codex/python312-runtime-contract`
- PR: `#81`
- Trigger artifact: `docs/reviews/pr-81-review-20260718-193522.md`
- Artifact path: `docs/gateflow/python312-runtime-contract-pr-review-fix-20260718-194155.md`
- Completion status: complete; ready for PR re-review

## Accepted Findings Fixed

### PR81-001 — bootstrap executable symlink containment

- Changed `scripts/python_runtime.sh` so `_om_python_command_path` resolves the executable file's symlink chain before canonicalizing its parent.
- The containment check now compares the final physical executable path against the final physical target-venv path.
- Added `test_bootstrap_selector_rejects_external_alias_to_target_interpreter` to prove that an outside alias whose final target is `.venv/bin/python` is rejected.
- Preserved the existing test for a symlinked target venv.

### PR81-002 — current RUNBOOK runtime/module drift

- Changed the automatic trade-intake row in `RUNBOOK.md` to:

  ```text
  ./.venv/bin/python -m src.application.trades.auto_intake --mode apply --yes
  ```

- Added `RUNBOOK.md` to the current operational docs contract test.
- Added assertions that current operational docs do not reintroduce bare ``python3 -m src.application`` commands and that the canonical trade-intake command is present.

## Changed Files

- `scripts/python_runtime.sh`
- `tests/test_python_runtime_contract.py`
- `RUNBOOK.md`
- `docs/reviews/pr-81-review-20260718-193522.md`
- `docs/gateflow/python312-runtime-contract-pr-review-fix-20260718-194155.md`

## Validation

- Focused runtime-contract tests: `10 passed`.
- Full pytest with the repository runtime-dependency venv temporarily linked into the clean worktree: `2679 passed, 10 skipped in 94.84s`.
- The temporary `.venv` link was removed after the run.
- An initial full-suite run without `.venv` produced 18 `FileNotFoundError` failures in tests that intentionally spawn `<repo>/.venv/bin/python`; this was a test-environment precondition, not a product failure, and the authoritative rerun passed.
- `bash -n` for runtime selectors/public shell entrypoints: pass.
- `ruff check .`: pass.
- Python 3.12 `compileall`: pass.
- Dependency graph check: 468 production modules, zero cycles.
- `git diff --check`: pass.

## Docs Decision

Update the current root operator runbook because it contained an executable, side-effecting command that bypassed the new runtime contract and referenced a retired module path. Historical artifacts remain unchanged.

## Residual Risks

- Direct execution of arbitrary internal files with generic shebangs remains outside supported public entrypoints; classification: accepted work-unit boundary.
- Exact Python 3.12 patch versions remain unpinned; classification: accepted minimum-version policy.
- Historical plan/review/memory snapshots retain old commands; classification: intentional evidence preservation.

No unclassified residual risk or blocking open question remains.
