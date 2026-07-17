# Gateflow Implementation Artifact — Option Performance Refactor S4

- **Gate**: implementation slice
- **Work unit**: `option-performance-refactor`
- **Slice**: S4 — Valuation/FX Evidence, Current Collection and Capture Core
- **Date**: 2026-07-17
- **Artifact path**: `docs/gateflow/option-performance-refactor-s4-implementation-20260717.md`
- **Completion status**: implementation pass; ready for S4 code review

## Objective and Outcome

S4 closes the current/historical option valuation loop without adding writes to report generation. The implementation now supports append-only valuation/FX evidence, explicit transactional migration/import, deterministic correction-aware selection, current-only read-through collection, replay-stable historical valuation, opening/ending unrealized PnL, effective-time CNY translation, and evidence provenance.

## Changed Files

### Domain contracts and engine

- `domain/domain/performance/models.py`
- `domain/domain/performance/engine.py`
- `domain/domain/performance/__init__.py`

Implemented versioned valuation/FX facts, envelope parsing, correction validation, seven-day staleness, deterministic selectors, boundary option position inputs, mark-based short/long unrealized formulas, gross/net completeness, selected evidence IDs, CNY translation, valuation-only scope, and `realized + ending - opening` period totals.

### Application and infrastructure

- `src/infrastructure/performance_evidence_sqlite.py`
- `src/application/performance/evidence_collection.py`
- `src/application/performance/adapters.py`
- `src/application/performance/service.py`
- `src/application/opend_market_snapshot_fetching.py`

Implemented no-DDL reads, explicit v1 migration, dry-run validation, one-transaction apply, idempotent append-only facts, structured identity verification, current-only collection, cross-account instrument reuse, stored-code conflict rejection, exact chain resolution, batched exact-code snapshots, timestamp preservation/fallback, no-write FX collection, capture envelope generation, legal ledger boundary projection, later-void restatement, and current/historical service provenance.

### Tests and documentation

- `tests/test_performance_evidence_sqlite.py`
- `tests/test_performance_evidence_collection.py`
- `tests/test_performance_valuation.py`
- `tests/test_performance_engine.py`
- `tests/test_performance_service.py`
- `docs/OPTION_PERFORMANCE_DESIGN.md`

Coverage includes schema no-mutation, migration idempotency, transaction rollback, correction identity/cycle/effective-time behavior, source priority, structured-key mismatch, weekend/staleness selection, exact batched snapshot collection, timestamp seams, conflicting stored codes, missing/crossed marks, current no-write behavior, historical no-live behavior, capture replay, later void restatement, valuation-only scope, breakdown conservation, observed zero, missing fee/FX quality, and opening/end period formulas.

## Decisions and Invariants

1. Native-currency maps remain authoritative; CNY is a derived evidence-backed amount.
2. Evidence reads never initialize or mutate schema.
3. Evidence apply owns migration and batch insertion in one transaction.
4. Corrections are append-only, exact-identity preserving, acyclic, and effective-time aware.
5. Historical reports never call live option/stock/FX sources.
6. Current report collection is read-through only and merges only into ending valuation.
7. Option boundary positions are projected through `src.application.ledger.api`; performance does not rematch lots.
8. Boundary inventory and period realized activity use the same canonical later-void restatement semantics.
9. Valuation-only positions participate in scope and account/symbol/month reductions.
10. Naive or future broker timestamps are not guessed; collection records timestamp fallback.

## Validation

Passed:

```text
python3 -m pytest \
  tests/test_performance_evidence_sqlite.py \
  tests/test_performance_evidence_collection.py \
  tests/test_performance_valuation.py \
  tests/test_performance_engine.py \
  tests/test_performance_service.py -q
# 35 passed

python3 -m pytest \
  tests/test_performance_period.py \
  tests/test_performance_models.py \
  tests/test_performance_instrument_identity.py \
  tests/test_performance_engine.py \
  tests/test_performance_evidence_sqlite.py \
  tests/test_performance_evidence_collection.py \
  tests/test_performance_valuation.py \
  tests/test_performance_service.py \
  tests/test_ledger_projection.py \
  tests/test_ledger_economics.py \
  tests/test_ledger_event_codec.py \
  tests/test_ledger_service.py \
  tests/test_ledger_sqlite_workflows.py -q
# 170 passed

python3 -m ruff check domain/domain/performance \
  src/application/performance \
  src/infrastructure/performance_evidence_sqlite.py \
  src/application/opend_market_snapshot_fetching.py \
  tests/test_performance_evidence_sqlite.py \
  tests/test_performance_evidence_collection.py \
  tests/test_performance_valuation.py \
  tests/test_performance_engine.py \
  tests/test_performance_service.py
# passed

git diff --check
# passed
```

## Docs Decision

`docs/OPTION_PERFORMANCE_DESIGN.md` now records the accepted S4 evidence schema/state machine, selectors, boundary replay semantics, valuation formulas, FX quality behavior, exact-code collection path, and current/historical no-write contract.

## Residual Risks and Uncovered Areas

| Risk / uncovered area | Classification |
|---|---|
| Assigned-stock identities are accepted by the collector seam but are not yet projected or valued in reports | covered by later approved S5 |
| Capital exposure and capital-days are absent | covered by later approved S6 |
| Public Agent/CLI import/capture commands are not wired | covered by later approved S7 |
| Legacy analysis consumers do not yet use the new internal service | covered by later approved S8 |
| Portfolio PnL/cash bridges are absent | covered by later approved S9 |
| Historical evidence coverage remains operationally sparse until explicit capture/import/backfill | covered by later approved S7/S10 operational workflow and explicit partial quality |
| General non-assignment stock inventory is outside this work unit | assigned to later work unit; current reports remain explicit rather than inventing support |

No unclassified residual risk remains.

## Next Entry Point

S4 code review using `deepreview`.
