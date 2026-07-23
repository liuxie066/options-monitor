# Gateflow Review Fix Artifact — S1 Close-aware Strategy Lab Build

- Gate: code review fix
- Work unit: `close-evidence-collection`
- Slice: S1
- Review: `docs/reviews/code-review-20260723-084455.md`
- Implementation artifact: `docs/gateflow/close-evidence-collection/s1-implementation.md`
- Artifact path: `docs/gateflow/close-evidence-collection/s1-review-fix.md`
- Completion status: fixes complete; pending re-review

## Finding decisions and fixes

### S1-DR-01 — accepted — 已修复

- Close build wrapper now catches `Exception`, not only `ValueError`.
- Every Close build failure still triggers the independent candidate build attempt and then re-raises the original Close error.
- Added regression coverage with an injected `OSError`; the call fails with that error while the valid candidate manifest is persisted.

### S1-DR-02 — accepted — 已修复

- Replaced manifest-only detection with an explicit facet status check.
- `complete` requires both a manifest `close_decision_facet` and all three `OPTIONAL_CLOSE_DATASET_FILES` as regular files.
- Incomplete persisted state returns `dataset_exists_without_complete_close_decisions`; candidate-only state retains `dataset_exists_without_close_decisions`.
- Added complete-idempotency coverage that preserves existing close marks and incomplete-facet coverage that does not overwrite the target.

## Validation

```text
75 passed in 2.35s
ruff: all checks passed
git diff --check: passed
```

## Docs decision

No S1 docs change. New operator behavior remains owned by S2 service/docs wiring.

## Residual risks

- 6h sampling coverage: `covered by later approved slice` S2 for docs and `assigned to later work unit` S5 for readiness evaluation.
- No automatic repair of incomplete existing datasets: `assigned to later work unit` only with production evidence; current behavior is explicit and non-destructive.
- Service enablement: `covered by later approved slice` S2.
