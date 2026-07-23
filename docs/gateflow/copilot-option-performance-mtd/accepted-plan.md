# Gateflow Accepted Plan — Copilot Option Performance MTD

- Work unit: `copilot-option-performance-mtd`
- Gate: `plan -> planreview -> fix -> re-review`
- Decision: `pass-with-risks`
- Plan: `docs/gateflow/copilot-option-performance-mtd/plan.md`
- Initial review: `docs/reviews/plan-review-20260723-164351.md`
- First re-review: `docs/reviews/plan-review-20260723-164617.md`
- Accepted re-review: `docs/reviews/plan-review-20260723-164741.md`

## Accepted findings

- Explicit invalid/empty inputs remain fail closed; only fake safe defaults are removed.
- The adapter does not prefill `period=mtd`; canonical domain default remains authoritative.
- Period pruning requires an explicitly supplied valid discriminator.
- Account provenance is not inferred by a new parser; actual report scope is always visible.

## Approved implementation boundary

1. Add one optional tool-owned Copilot input normalizer.
2. Fix option-performance safe defaults and period-specific payload canonicalization.
3. Add exact option-versus-assigned-stock realized metrics in the performance engine.
4. Improve deterministic rendering, model guidance, and exact online-conversation regression.
5. Do not alter ledger/accounting semantics, production config/data, notifications, or runtime
   state.

## Residual risks

- Current-message-only account provenance is not available in the scene contract.
- Period-total PnL remains combined and must be labeled as such.
- No live Feishu canary is included.

## Next gate

Commit the accepted plan artifacts, then implement Slice 1 and run its focused validation before
code review.
