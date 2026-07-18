# Gateflow Fix Artifact — Option Performance Refactor S10

- **Gate**: fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S10 — Shadow Reconciliation, Cutover, Docs and Legacy Isolation
- **Created at**: 2026-07-18 09:22:55 CST
- **Status**: fix-complete; awaiting re-review
- **Artifact path**: `docs/gateflow/option-performance-refactor-S10-fix-20260718-092255.md`
- **Source review**: `docs/reviews/code-review-20260718-091848.md`

## Accepted Finding Fixes

### F1 — unreadable facts must not become proven zero

- Added `_can_apply_proven_zero_semantics`.
- Recursive zero promotion now runs only when report-level diagnostic warnings are empty.
- Decode/projection/evidence warnings keep absent amount envelopes `not_observed` instead of asserting observed zero.
- Added an integration test with an undecodable in-period event; the report remains partial and monetary metrics are not promoted.

### F2 — realized identity reconciliation must fail closed

- Identity reconciliation now requires explicit list-valued `rows` payloads on both legacy and v1 reports.
- Omitted/malformed rows fail with the missing side identified.
- One-sided realized identities are compared and fail through the exact delta.
- Explicitly present empty detail sets reconcile as an empty exact identity map rather than an unchecked `not_applicable` result.

### F3 — coverage must traverse breakdown lists

- `_amount_envelopes` now recursively traverses lists and tuples.
- Indexed failure paths are emitted, for example `breakdowns.monthly[0].pnl.realized_net`.
- Added a missing-fee-as-zero breakdown regression test.

### F4 — incomplete fee evidence is not an actual fee delta

- Fee classification now requires native gross/net currency coverage to match and rejects non-FX missing evidence.
- Missing fee/currency evidence returns `fee_coverage_incomplete` and no numeric delta.
- FX-only partial translation can still retain a valid native actual-fee comparison.

### F5 — observed non-CNY metrics require CNY/FX evidence

- Coverage rejects any observed amount envelope with `cny=None`.
- Observed envelopes with non-CNY native currencies must carry selected FX fact IDs.
- Added regression coverage for observed USD with erased FX provenance.

## Changed Files

- `src/application/performance/service.py`
- `src/application/performance/reconciliation.py`
- `tests/test_performance_reconciliation.py`

## Validation

- `python3 -m pytest tests/test_performance_reconciliation.py tests/test_option_performance_agent_tool.py -q` → **24 passed**.
- `python3 -m pytest tests/test_performance_service.py tests/test_performance_engine.py tests/test_performance_assignment.py tests/test_performance_reconciliation.py tests/test_option_performance_agent_tool.py -q` → **46 passed**.
- `python3 -m ruff check src/application/performance/service.py src/application/performance/reconciliation.py tests/test_performance_reconciliation.py` → **passed**.
- `git diff --check` for the fix files → **passed**.

## Docs Decision

No public contract wording changed. Existing docs already state the corrected invariants: missing evidence is not zero, exact reconciliation fails closed, and missing fee/FX evidence remains explicit.

## Residual Risks

- **fixed in current slice**: F1-F5.
- **fail-closed current policy**: UTC/Asia-Shanghai boundary mismatches still require source-fact investigation; they are not automatically excused by the timezone classification.
- **assigned to later work unit**: legacy adapter removal after the deprecation window.
- **assigned to operational follow-up**: historical valuation/FX evidence backfill.

## Completion Status

- **Gate result**: fix-complete
- **Next Gateflow entry point**: S10 re-review
