# Gateflow S6 Fix — Option Performance Refactor

- **Gate**: code review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S6 — Capital Exposure and Efficiency
- **Created at**: 2026-07-17 18:40:51 UTC
- **Finding source**: `docs/reviews/code-review-20260717-183745.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s6-fix-20260717-184051.md`
- **Decision**: accept and fix S6-CR-01 through S6-CR-03

## Accepted Findings and Fixes

### S6-CR-01 — Exact denominator before presentation rounding

- Annualized efficiency now receives the exact Decimal aggregate, not `rounded_sums`.
- Capital-day presentation precision was increased to 12 decimal places so low-notional millisecond exposure remains externally reproducible.
- Added a 1ms, USD 1-notional regression proving the exact denominator produces the expected annualized result.
- **Status**: 已修复。

### S6-CR-02 — Preserve heuristic covered-call quality

- Capital construction now reads `allocation_status` from projector-owned covered-call allocation rows.
- Any non-explicit attribution produces a coverage warning and `coverage.status=partial` while retaining the validated zero-incremental numeric treatment.
- Capital warnings also propagate to top-level report quality.
- Added an explicit FIFO coverage downgrade test.
- **Status**: 已修复。

### S6-CR-03 — Known zero cost is not missing

- Capital segments now permit known zero notional while continuing to reject negative values.
- Long options with zero opening premium and assigned-stock zero cost basis can form observed zero-capital exposure.
- Zero-denominator efficiency remains `not_applicable`, distinct from missing basis.
- Added a zero-premium long-option regression.
- **Status**: 已修复。

## Validation

```text
python3 -m pytest tests/test_performance_capital.py tests/test_performance_engine.py -q
23 passed

python3 -m pytest tests/test_assigned_stock_projection.py tests/test_performance_assignment.py tests/test_positions_reporting.py -q
44 passed

python3 -m ruff check <S6 Python paths>
All checks passed

git diff --check -- <S6 paths and artifacts>
passed
```

## Docs Decision

No further design-doc change was needed: the existing S6 section already states exact Decimal aggregation, coverage quality, and explicit zero-denominator behavior. This fix aligns code with that documented contract.

## Residual Risks and Uncovered Areas

| Risk | Classification |
|---|---|
| Public payload row limiting for capital segments | covered by later approved slice S7 |
| Legacy assigned-stock efficiency output remains separate | covered by later approved slice S10 |
| NAV/margin/general stock capital | approved non-goal; assigned to later work unit |

No unclassified residual risk remains.

## Completion Status

- **Fix gate**: pass
- **Blocking open questions**: none
- **Current gate / next entry point**: S6 code re-review using `deepreview`
