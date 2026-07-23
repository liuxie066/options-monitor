# Gateflow Implementation Artifact — S1 Close-aware Strategy Lab Build

- Gate: implementation slice
- Work unit: `close-evidence-collection`
- Slice: S1 — Close-aware Strategy Lab build contract
- Plan: `docs/gateflow/close-evidence-collection-plan-20260723.md`
- Artifact path: `docs/gateflow/close-evidence-collection/s1-implementation.md`
- Completion status: implementation and re-review complete; ready for accepted slice commit

## Scope implemented

- Added independent latest non-empty Close Advice run discovery in the Shadow Replay capture owner.
- Added opt-in `include_close_decisions` to Strategy Lab update and the public CLI.
- Added close-first dataset build while preserving the existing candidate build contract.
- Isolated strict Close failure from candidate evidence accumulation: candidate build still runs, then the original Close error is re-raised.
- Added explicit close-specific build result and summary fields while keeping existing singular summary fields candidate-only.
- Aggregated safety/status across either local dataset write.
- Added CLI validation for ambiguous/ineffective argument combinations.

## Changed files

- `src/application/shadow_replay/capture.py`
- `src/application/strategy_lab/update.py`
- `src/interfaces/cli/research.py`
- `tests/test_strategy_lab.py`
- `tests/test_research.py`

## State and error handling evidence

- Empty/header-only Close files are skipped by discovery and do not fail the recorder.
- Non-empty malformed evidence still fails in `capture_close_decision_episodes()` before a valid close dataset is written.
- Valid same-run close/candidate evidence creates one close-aware dataset; the candidate builder then idempotently skips the same run ID.
- Distinct close/candidate runs create two independent datasets.
- An existing candidate-only target is never overwritten and returns `dataset_exists_without_close_decisions`.
- Dry-run selects both sources but creates no dataset directory.
- Existing `dataset_built` / `built_dataset_id` remain candidate-only; new close-specific fields report close build state.

## Validation

```text
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_close_advice_shadow_capture.py tests/test_strategy_lab.py tests/test_research.py
75 passed in 2.35s

python3.12 -m ruff check \
  src/application/shadow_replay/capture.py src/application/strategy_lab/update.py \
  src/interfaces/cli/research.py tests/test_research.py tests/test_strategy_lab.py
All checks passed!

git diff --check
passed
```

## Docs decision

Public/operator docs are intentionally deferred to approved S2, where the recorder service command and profile contract are wired.

## Residual risks and uncovered areas

- 6h sampling density: `covered by later approved slice` for documentation in S2, then `assigned to later work unit` for S5 readiness coverage evaluation.
- Existing candidate-only close collision: `assigned to later work unit` only if production evidence shows material loss; current slice reports and preserves it.
- Active-run partial write race: `requiring new issue or explicit user decision` only if production canary/runtime evidence proves recurrence.
- Systemd/launchd automatic enablement: `covered by later approved slice` S2.

## Stop condition

S1 code, focused validation and deepreview loop are complete; no accepted unresolved finding remains.
