# Code Review — Daily Decision Brief S2

- **Gate**: code review
- **Work unit**: `daily-decision-brief`
- **Slice**: S2
- **Selected base**: accepted S1 commit `0c78fbfb`
- **Reviewed target**: S2 workspace diff
- **Date**: 2026-07-19
- **Reviewer mode**: `deepreview` current-changes
- **Deepreview artifact**: `docs/reviews/code-review-20260719-175921.md`
- **Status**: findings accepted; fix required
- **Artifact path**: `docs/gateflow/daily-decision-brief-s2-code-review-20260719.md`

## Findings

| Finding | Severity | Decision | Required fix |
|---|---|---|---|
| CR-S2-1 pipeline failure can become LIVE | High | accepted | Consume authoritative pipeline-completion fact, not `ran_scan`. |
| CR-S2-2 delivery confirmation accepts wrong envelope | High | accepted | Validate kind/key against immutable run diff and referenced base revision. |
| CR-S2-3 malformed delivery fields escape unavailable | Medium | accepted | Normalize all pointer failures to repository state error. |
| CR-S2-4 rejection summary crosses market boundary | Medium | accepted | Aggregate only market-filtered trace/reject rows. |
| CR-S2-5 orphan current state is trusted | Medium | accepted | Verify current against immutable revision artifact/digest before increment. |

## Direct validation evidence

- Wrong `delivery_kind=delta` / `delivery_key=wrong-key` advanced a full revision pointer.
- Schema-valid pointer with `revision="bad"` escaped as bare `ValueError`.
- HK brief built from mixed trace reported both NVDA and `0700.HK` rejections.
- Production account failure path records `ran_scan=true` while the authoritative `AccountRunOutcome.ran_pipeline=false`.

## Open Questions

无。

## Residual risks / uncovered areas

- Multi-file crash-point recovery remains covered by approved S5 scenario closure.
- Provider idempotency support remains assigned to later production observation.
- No unclassified residual risk after accepting all findings into the S2 fix loop.

## Gate decision

- **Decision**: fail pending fix.
- **Current gate**: S2 fix.
- **Next entry point**: implement CR-S2-1..CR-S2-5, add regressions, validate, then re-review.
