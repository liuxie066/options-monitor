# Gateflow S6 Implementation — Option Performance Refactor

- **Gate**: implementation
- **Work unit**: `option-performance-refactor`
- **Slice**: S6 — Capital Exposure and Efficiency
- **Created at**: 2026-07-17 18:33:49 UTC
- **Artifact path**: `docs/gateflow/option-performance-refactor-s6-implementation-20260717-183349.md`
- **Completion status**: implementation complete; next gate is S6 code review using `deepreview`

## Scope and Decisions

Implemented continuous-time capital integration under the explicit basis `notional_days_v1`:

- exact `[start_at_ms, end_at_ms)` exposure intervals;
- Decimal `notional * overlap_ms / 86_400_000` aggregation;
- short-put strike notional;
- long-option opening premium debit;
- assigned-stock remaining cost basis with exact sale-time reductions;
- assignment handoff from put to stock at one timestamp with no gap or overlap;
- validated covered calls as zero incremental capital;
- naked/unallocated short calls as explicit unavailable coverage;
- realized-net and period-total-net annualized efficiency by native currency;
- explicit zero-denominator, missing-net-PnL, unsupported inventory, and invalid timeline states.

The output adds a `capital` section with basis, native-currency capital-days, two qualified efficiency envelopes, coverage, and auditable exposure segments. No NAV/margin return, integer-day approximation, or unqualified `return_rate` was introduced.

## Changed Files

- `domain/domain/performance/models.py`
  - added the immutable `CapitalExposureSegment` contract and exact overlap/capital-day calculation.
- `domain/domain/performance/engine.py`
  - added capital timeline construction, aggregation, efficiency calculation, coverage propagation, and report serialization.
- `domain/domain/assigned_stock.py`
  - exposes projector-owned `covered_call_allocations` so the performance engine consumes validated attribution instead of duplicating it.
- `tests/test_performance_capital.py`
  - new boundary/conservation suite for same-day, partial close, midnight, cross-period, assignment, stock sale, covered call, long option, zero denominator, missing net PnL, and quantity-time conservation.
- `tests/test_assigned_stock_projection.py`
  - validates the new projector output and fail-closed mixed-inventory behavior.
- `docs/OPTION_PERFORMANCE_DESIGN.md`
  - documents the capital contract and marks S6 implemented.

## Approved-Plan Deviation

S6's original allowed-file list did not include `domain/domain/assigned_stock.py` or
`tests/test_assigned_stock_projection.py`. The implementation deliberately made a minimal ownership-preserving extension to the shared projector because S5 already owns the covered-call attribution state machine. Reimplementing that logic in the performance engine would duplicate explicit-link, FIFO, mixed-inventory, reservation, and sale-interval rules and could classify naked calls incorrectly. The extension is read-only output of the projector's existing decision; it does not change attribution behavior.

## Validation

```text
python3 -m pytest tests/test_performance_capital.py tests/test_performance_engine.py -q
20 passed

python3 -m pytest tests/test_assigned_stock_projection.py tests/test_performance_assignment.py tests/test_positions_reporting.py -q
44 passed

python3 -m ruff check domain/domain/performance/models.py domain/domain/performance/engine.py domain/domain/assigned_stock.py tests/test_performance_capital.py tests/test_assigned_stock_projection.py
All checks passed

git diff --check -- <S6 paths>
passed
```

## Docs Decision

Updated `docs/OPTION_PERFORMANCE_DESIGN.md` because S6 adds a public report section and precise calculation/coverage semantics.

## Residual Risks and Uncovered Areas

| Risk | Classification |
|---|---|
| Public Agent/CLI rendering and compatibility behavior for the new capital section | covered by later approved slice S7 |
| Analysis/Copilot consumers may still prefer legacy return fields | covered by later approved slice S8 |
| Portfolio PnL/cash bridges do not yet consume the new report | covered by later approved slice S9 |
| Legacy assigned-stock `capital_days` fields still exist in the legacy projection output | covered by later approved slice S10 legacy isolation; the new report never consumes them |
| General stock inventory and margin/NAV capital bases remain unsupported | assigned to later work unit by approved non-goals; current output fails closed |

No unclassified residual risk remains.
