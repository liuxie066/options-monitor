# Gateflow Fix Artifact — Sell Put Top1 HK Terminal Fee PlanReview

- Gate: `fix`
- Work unit: `sell-put-top1-hk-terminal-fee-contract`
- Review artifact: `docs/reviews/plan-review-20260815-031318.md`
- Fixed target: `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/plan.md`
- Artifact path: `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/plan-fix.md`
- Status: `fix complete; pending re-review`

## Finding decisions and fixes

### PR-HKF-01 — accepted — fixed

- Kept the pure calculator's explicit plan input for a future receipt-bound caller.
- Removed permission for the two current consumers to trust ordinary event/position `account_fee_plan` mappings.
- Required both consumers to remain missing unless actual broker fee evidence exists; a validated intake remains separate W0R work.

Final status: `已修复`.

### PR-HKF-02 — accepted — fixed

- Required the assigned-stock fee path to preserve raw shares/price through the strict calculator boundary.
- Prohibited reuse of lossy `int(float(...))` parsing for this fee path.
- Added consumer-level bool/fraction/non-finite regressions to the validation contract.

Final status: `已修复`.

### PR-HKF-03 — accepted — fixed

- Frozen one exact result key set for every terminal kind and state.
- Defined HKD units, six-decimal rounding, nullable fields, and component-sum invariants.
- Kept the contract as a plain dict; no dataclass or schema framework was added.

Final status: `已修复`.

## Residual risks

- Real account receipt/intake remains W0R and cannot be inferred from this source contract.
- Per-order aggregation and tiered pricing require already resolved plan facts from that future owner.

## Completion status

`fix complete`; next entry point: `plan re-review`.
