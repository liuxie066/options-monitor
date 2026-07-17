# Gateflow Fix Artifact — option-performance-refactor S7

## Gate

- Slice: S7
- Gate: fix after code review
- Review artifact: `docs/reviews/code-review-20260717-233722.md`
- Status: all accepted findings fixed; pending re-review
- Artifact: `docs/gateflow/option-performance-refactor-s7-fix-20260717-233936.md`

## Finding Decisions and Fixes

- `S7-CR-01` — **accepted / 已修复**. Evidence import now accepts only config/store location parameters; account/broker remain available only on report/capture where they are actually applied. Added argparse rejection coverage.
- `S7-CR-02` — **accepted / 已修复**. Domain close economics now leave net PnL null until a fee is successfully calculated. Runner fills gross/fee/net together; fee-unavailable coverage asserts net remains null.
- `S7-CR-03` — **accepted / 已修复**. Legacy `as_of_ms` maps to an `Asia/Shanghai` MTD `as_of_date`, forces no-live refresh, and emits a semantic warning. Month + timestamp emits an explicit natural-month cutoff warning instead of silent behavior.
- `S7-CR-04` — **accepted / 已修复**. Agent output contract now names the real capital efficiency fields and contract tests pin both paths.

## Validation

```text
S7 focused validation: 236 passed
Ruff focused checks: passed
git diff --check: passed
```

## Residual Risks

- S8 consumer migration, S9 bridge migration, and S10 reconciliation/full-suite/docs remain covered by their approved later slices.
- No unclassified S7 residual risk remains.
