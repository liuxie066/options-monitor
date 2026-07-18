# Gateflow Implementation Artifact — release-upgrade-slice1

## Gate

implementation

## Work unit and scope

- Work unit: release upgrade optimization, approved Slice 0 + Slice 1 only
- Base: `origin/main` `b5ba7693`, VERSION `1.2.413`
- Branch: `codex/release-upgrade-slice1`
- Production code/config/service changes: none

## Baseline

Using `OM_PYTHON=python3` because the ignored repository `.venv` lacks pytest:

- Full preflight elapsed: 108s / 103s / 106s; median 106s
- Full pytest elapsed: 101.60s / 98.09s / 101.16s; median 101.16s
- Test result: `2680 passed, 10 skipped`
- Slow pair: 30.03s + 30.03s; combined 60.41s

## Changed files

- `tests/test_phase1_tool_boundary.py`
  - Replaces the real rate-limit wave sleep with a recording monkeypatch.
  - Adds an assertion that the requested cooldown remains exactly `30.0` seconds.
- `tests/test_fetch_market_data_opend_explicit_expirations.py`
  - Wraps the snapshot helper and injects a direct rate-limited call for this error-reporting test.
  - Preserves gateway rate-limit failure and error payload assertions without wall-clock waiting.
- `scripts/release_preflight.sh`
  - Runs focused agent/plugin tests only in non-full mode.
  - Full mode runs the complete pytest suite once.
- `tests/test_release_test_plan.py`
  - Adds a fake-Python command recorder for the shell preflight.
  - Proves full mode executes one `pytest` command and non-full mode retains focused tests.

## Validation

Focused validation:

- 36 tests passed in 0.85s.
- Target test calls: approximately 0.01s each; no real 30-second wait.
- `bash -n scripts/release_preflight.sh`: passed.
- `git diff --check`: passed.

Full performance validation with dirty-worktree allowance during implementation:

- Full preflight elapsed: 40s / 38s / 38s; median 38s.
- Full pytest elapsed: 37.04s / 35.12s / 35.10s.
- Test result: `2682 passed, 10 skipped` on each run.
- Median improvement: 106s → 38s, approximately 64%.

A clean `--require-clean` run remains required after the accepted slice commit.

## Docs decision

The durable plan, goal confirmation, plan review, implementation artifact, and code review artifact are tracked under `docs/`. No public CLI, payload, config, output path, or safety boundary changed, so no operator handbook change is required.

## Residual risks

- The local ignored `.venv` is Python 3.12-compatible but lacks pytest; `release_preflight.sh` selects it before PATH Python. This is pre-existing and assigned to a later Python-runtime/preflight work unit, not fixed in this narrow slice.
- Timeout/dependency/shared-venv/CI/uv risks are covered by later plan slices and not implemented here.

## Completion status

Implementation complete; pending code review gate and clean-worktree validation.
