# Gateflow S1 Implementation — Canonical Attribution Provenance

- Gate: implementation
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Slice: S1
- Date: 2026-07-18
- Status: implementation complete; pending code review
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-s1-implementation-20260718.md`

## Scope

Implemented additive, immutable strategy attribution provenance for Option Performance facts, valuation positions and capital segments.

## Changed files

- `domain/domain/performance/attribution.py`
- `domain/domain/performance/models.py`
- `domain/domain/performance/engine.py`
- `src/application/performance/adapters.py`
- `tests/test_performance_attribution.py`

## Decisions

- Existing ledger metadata precedence is reused: `strategy_snapshot` first, top-level fallback.
- Non-empty conflicts fail closed for management attribution and are exposed as `attribution_issues`.
- Current V1 recognizes only `combo_yield` roles `funding_put` and `participation_call`.
- Lifecycle IDs use immutable lot identities: `funding_cycle:<lot_id>` and `participation:<lot_id>`.
- Canonical monetary facts, amounts, fact IDs and capital formulas are unchanged.
- Allocation realized facts consume existing canonical `OptionEconomicAllocation` metadata; no lot rematching was introduced.

## Validation

```text
python3 -m ruff check ...
All checks passed!

python3 -m pytest tests/test_performance_attribution.py tests/test_performance_engine.py tests/test_performance_models.py tests/test_performance_capital.py tests/test_performance_valuation.py -q
43 passed
```

## Docs decision

No authoritative public docs updated in S1; the public attribution object is introduced in later slices. S1 implementation semantics are covered by the accepted plan artifacts.

## Residual risks and uncovered areas

| Risk | Classification |
|---|---|
| Top-level attribution reducer/quality does not yet consume `attribution_issues` | covered by approved S2 |
| Group topology across reports starting after opens | covered by approved S2 all-event topology builder |
| Assigned-stock explicit group provenance | covered by approved S2 |
| Multiple funding cycles | assigned to later work unit |
| Intra-period Call PnL split without transition mark | assigned to later evidence-capture work unit |

## Completion status

S1 implementation complete; code review required before accepted slice commit.
