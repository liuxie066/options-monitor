# Gateflow S2 Fix — Attribution Scope and Conservation

- Gate: fix
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Slice: S2
- Findings: S2-01 through S2-04
- Date: 2026-07-18
- Status: fixed; pending re-review
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-s2-fix-20260718.md`

## Fixes

- Explicit non-Combo strategies now produce observed-empty attribution without issues.
- Ready topology groups are emitted only when period facts or capital segments make them relevant to the requested window.
- Conservation now verifies `period_total_gross` and `period_total_net` independently from source realized/opening/ending facts.
- Assigned-stock assignment, valuation, partial-sale PnL and capital use explicit canonical `stock_lot_id` as one stable lifecycle identity.
- Assigned-stock attribution remains explicit-only; no heuristic ownership was added.

## Validation

```text
ruff: pass
focused performance and assignment pytest: 57 passed
```

## Residual risks

| Risk | Classification |
|---|---|
| Exact intra-period transition PnL split | assigned to later evidence-capture work unit |
| Multiple Funding Put rolls | assigned to later work unit |
| Historical stock rows without explicit group provenance | data repair/later work unit; current attribution fails closed |
| Public contract/docs | covered by approved S3 |
