# Gateflow S2 Implementation — Strategy Attribution Reducer

- Gate: implementation
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Slice: S2
- Date: 2026-07-18
- Status: implementation complete; pending code review
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-s2-implementation-20260718.md`

## Scope

Added the additive `option_strategy_attribution.v1` domain reducer and integrated it into `PeriodPerformance`.

## Changed files

- `domain/domain/performance/strategy_attribution.py`
- `domain/domain/performance/engine.py`
- `tests/test_performance_strategy_attribution.py`

## Implemented behavior

- Builds one-Put/one-Call topology from all scoped effective canonical events and allocations.
- Separates Funding Put cycle PnL/capital from Participation Call lifecycle PnL/capital.
- Emits group-lifetime funding snapshot without mutating period cash or PnL.
- Computes risk capital-days, average incremental capital and scope-matched net annualized efficiency.
- Emits residual-tail capital and only exposes tail PnL when the report begins after the Put fully closes.
- Emits native-currency conservation for attributed PnL source facts.
- Invalid topology is attribution-partial while canonical totals remain unchanged.

## Validation

```text
ruff: pass
focused performance pytest: 44 passed
residual-tail focused pytest: 4 passed
```

## Docs decision

Authoritative public docs remain scheduled for S3 after the reducer contract passes review.

## Residual risks

| Risk | Classification |
|---|---|
| Public Agent/CLI snapshots and docs | covered by approved S3 |
| Exact intra-period Call tail split | assigned to later evidence-capture work unit; V1 explicit partial |
| Assigned-stock group continuation | explicit provenance-only support remains incomplete and requires review/fix or later approved slice classification |
| Multiple Funding Put rolls | assigned to later work unit |
