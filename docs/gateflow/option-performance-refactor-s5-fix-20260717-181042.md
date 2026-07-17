# Gateflow S5 Fix — Option Performance Refactor

- **Gate**: code review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S5
- **Created at**: 2026-07-17 18:10:42 UTC
- **Finding source**: `docs/reviews/code-review-20260717-175953.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s5-fix-20260717-181042.md`
- **Decision**: accept and fix S5-CR-01 and S5-CR-02

## Fixes

### S5-CR-01 — Covered-call interval coverage

- Added minimum available assigned-stock share calculation across the full half-open call coverage interval.
- Checkpoints include sale times and overlapping reservation transitions.
- A sale before call close/as-of now makes attribution fail closed with `covered_call_unallocated` instead of reporting a covered lifecycle.
- Added a focused regression for call open -> assigned-stock sale -> call still open.
- **Status**: 已修复。

### S5-CR-02 — Settlement fee provenance

- `_stock_settlement_facts` now requires `fee_provenance.basis=actual` before exposing a fee cash amount.
- Actual zero remains an observed native-currency amount.
- Estimated/missing/unprovenanced settlement fees produce explicit missing reasons, preserving gross principal while making fee and total-cash quality partial.
- Added estimated-vs-actual-zero regression coverage and made the legacy principal test carry explicit actual-zero stock fee provenance.
- **Status**: 已修复。

## Validation

- `python3 -m pytest tests/test_assigned_stock_projection.py tests/test_performance_assignment.py tests/test_performance_engine.py -q`: 21 passed.
- Full S5 plus engine/service compatibility: 72 passed.
- S1-S4 performance/evidence compatibility: 62 passed.
- Ruff: passed.
- `git diff --check`: passed.

## Residual Risks

All residual risks remain classified in the S5 implementation artifact. Neither fix expands into S6 capital metrics or general stock inventory.

## Completion Status

- Fix gate: pass
- Blocking open questions: none
- Next entry point: S5 code re-review using deepreview
