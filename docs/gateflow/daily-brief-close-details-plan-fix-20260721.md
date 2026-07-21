# Gateflow Plan Fix: Daily Brief Close-Position Details

## Gate

- Work unit: `daily-brief-close-details`
- Gate: plan review fix
- Finding: `PR-001`

## Decision

Accepted. `realized_if_close` is signed net P&L and may be negative. The plan now requires sign-sensitive wording:

- non-negative: `预计锁定收益`;
- negative: `预计平仓损益`.

The validation scope now includes a negative realized-P&L renderer regression.

## Changed Artifact

- `docs/gateflow/daily-brief-close-details-plan-20260721.md`

## Residual Risks

- Signed-value semantic risk: fixed in current slice through the revised plan and required test.
- No unclassified residual risk.

## Status

- `PR-001`: 已修复
- Next gate: plan re-review
