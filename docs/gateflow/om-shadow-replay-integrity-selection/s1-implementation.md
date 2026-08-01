# Gateflow Implementation — S1 integrity eligibility

- Gate: implementation
- Work unit: `om-shadow-replay-integrity-selection`
- Slice: S1
- Base: accepted plan commit `26270b6b`
- Status: implemented; pending code review

## Changed files

- `src/application/shadow_replay/status.py`
- `src/application/shadow_replay/data_plan.py`
- `src/application/shadow_replay/collection.py`
- `tests/test_shadow_replay.py`
- `tests/test_strategy_lab.py`
- `docs/STRATEGY_LAB_DESIGN.md`
- `docs/DEPENDENCY_GRAPH.md`

## Decisions and behavior

- Data-plan rows carry the status owner's compact dataset-integrity fact.
- Write-mode execution skips any status other than exact `verified` before the
  OpenD circuit and `max_datasets` counter.
- Action receipts expose integrity status/reason and summaries count integrity
  skips.
- Direct write-mode collection calls the existing integrity validator before
  dataset reads, OpenD fetch, or persistent cache setup.
- Dry-run planning remains readable and unchanged in eligibility behavior.
- Historical datasets are not rewritten or given synthesized manifests.

## Validation

- Focused Shadow Replay/Strategy Lab/Research: `125 passed`.
- Ruff on changed Python/test files: passed with `--no-cache` (the repository
  `.ruff_cache` is filesystem-protected in this environment).
- Dependency graph regenerated: `production_modules=576 cycles=0`.
- Dependency graph check: passed.
- `git diff --check`: passed.
- Full suite: pending after code review.

## Docs decision

Updated Strategy Lab's operator contract for verified-only write selection and
direct fail-early behavior. Regenerated the dependency graph for the added
application-to-common import.

## Residual risks

- Legacy evidence remains read-only and needs trusted regeneration to become a
  write target; assigned to normal evidence production, outside this work unit.
- Real OpenD behavior is not proven by unit tests; covered by the authorized
  post-upgrade production canary.

## Completion signal

S1 implementation is complete. Next entry point: code review.
