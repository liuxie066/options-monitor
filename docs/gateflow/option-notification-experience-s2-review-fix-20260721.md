# Gateflow Slice 2 Review Fix

- Gate：Slice 2 fix / re-review
- Work unit：期权监控通知体验升级
- Review artifact：`docs/reviews/code-review-20260721-184937.md`
- Fixed at：2026-07-21 18:52:07 CST

## Accepted findings and fixes

### DR-S2-01 — candidate_index representative eligibility

- Decision：accepted
- Final status：已修复
- Fix：domain normalizer now validates explicit candidate representatives with the same minimum contract as the assembler: canonical capacity at least one contract plus family-specific required contract fields.
- Compatibility：legacy briefs without `candidate_index` still use best-effort action derivation; action `metrics.capacity` is promoted into the bounded representative, and insufficient legacy combo evidence is skipped rather than fabricated.
- Verification：malformed empty/zero-capacity representatives fail closed; repository and scenario repeated-normalization regressions pass.

### DR-S2-02 — non-live briefs could expose derived alertable identities

- Decision：accepted
- Final status：已修复
- Fix：`candidate_index` is now structurally valid only for `live_actionable` briefs. Planning/blocked briefs without the field normalize to an empty index; a non-empty explicit index on a non-live brief fails closed.
- Verification：planning legacy action does not derive an identity; explicit planning index is rejected.

## Validation

- Full Slice 2 + Daily Brief repository/notification/scenario/agent/CLI suite：`137 passed in 1.28s`。
- Ruff：pass。
- No unrelated files modified.

## Residual risks

- Old revision digest migration：covered by later approved Slice 3。
- Success-only current persistence：covered by later approved Slice 3。
- User-visible render/query：covered by later approved Slice 5。

## Completion status

- Accepted findings fixed：2/2
- Unclassified residual risks：0
- Current gate / next entry point：Slice 2 re-review
