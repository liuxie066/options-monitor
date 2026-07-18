# Gateflow S1 Fix — Stable Close Lifecycle Identity

- Gate: fix
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Slice: S1
- Finding: S1-01
- Date: 2026-07-18
- Status: fixed; pending re-review
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-s1-fix-20260718.md`

## Fix

- Open events continue to derive lifecycle identity from canonical `lot_id_for_open_event`.
- Close-like events now derive lifecycle identity from `target_lot_id`.
- Tagged close events without a target lot fail closed with `strategy_lifecycle_source_missing:<event_id>` instead of inventing a close-event lifecycle.
- Added parity tests for close cash facts and fail-closed orphan closes.

## Validation

```text
ruff: pass
focused pytest: 45 passed
```

## Residual risks

- Topology/group reducer remains covered by approved S2.
- No unclassified residual risk in S1.
