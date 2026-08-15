# Gateflow Plan Fix — Sell Put Top1 W5 Evaluator Slice

- Gate: `plan review -> fix`
- Work unit: `sell-put-top1-w5-evaluator`
- Review artifact: `docs/reviews/plan-review-20260815-154327.md`
- Artifact path: `docs/gateflow/sell-put-top1-w5-evaluator/plan-fix.md`
- Status: fix complete; re-review passed

## Finding decisions and fixes

### PR-W5-01 — accepted — 已修复

The plan now requires every baseline/challenger candidate that actually enters HK expiry economics to use `currency=HKD`. A compared non-HKD candidate produces `insufficient_evidence / ranking_projection_incomplete / candidate_currency_mismatch`, never reaches W1B economics, and cannot produce a leader. No currency-conversion behavior was added. A dedicated regression is required.

### PR-W5-02 — accepted — 已修复

The plan now defines:

- the exact `variant_result`, daily-delta, and `missing_receipts` key sets;
- overall `effective_days` as null before statistics and otherwise the minimum across authorized variants;
- stable first-seen reason aggregation in authorized variant order;
- lexically sorted pre-statistics reasons/missing tuples;
- normal insufficient behavior for a duplicate required close and no effect from structurally valid unused duplicates;
- the exact `ResearchEvaluationError.reason_code` set;
- required deterministic-output assertions.

## Validation

- The confirmed pure-evaluator boundary is unchanged.
- No provider, storage, loader, runner, state, or currency-conversion scope was added.
- `git diff --check` remains required before accepted-plan commit.

## Residual risks

- Raw artifact byte verification: assigned to the remaining W5 runner.
- Real provider/fee/calendar/quota evidence: assigned to W0R remediation and the remaining W5/W7 work.
- Real evaluation publication into M3 terminal history: assigned to the remaining W5 runner.

All residual risks are classified; none blocks plan re-review.

## Next gate

`accepted plan commit -> implementation S1`
