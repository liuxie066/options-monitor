# Gateflow S5 Implementation — Option Performance Refactor

- **Gate**: implementation slice S5
- **Work unit**: `option-performance-refactor`
- **Slice**: S5 — Sell Put Assigned-Stock Lifecycle
- **Created at**: 2026-07-17 18:10:41 UTC
- **Artifact path**: `docs/gateflow/option-performance-refactor-s5-implementation-20260717-181041.md`
- **Completion status**: implementation complete; code review findings fixed and awaiting accepted slice commit

## Scope and Decisions

- Added the legal assigned-stock event read boundary `assigned_stock_event_log(repo)` in `src.application.ledger.queries` and re-exported it from `ledger.api`.
- Moved Sell Put assignment lot, sale, quote, fee, holdings reconciliation, lifecycle and covered-call attribution into `domain.domain.assigned_stock.project_assigned_stock_lifecycle`.
- Converted legacy `positions.reporting` into a delegating adapter and removed its duplicate assigned-stock semantic implementation.
- Integrated opening/ending assigned-stock projections into the performance service and pure engine.
- Reused S4 valuation evidence for assigned stock and open covered calls; historical paths remain no-live and current collection receives deduplicated stock instruments.
- Kept option premium and covered-call option economics owned by canonical option facts. Stock facts add only stock principal price movement, stock sale cash/fees, settlement fee impact and stock inventory activity.
- Added explicit principal-basis fields so assignment settlement fee is not embedded again in stock gross PnL.
- Required actual fee provenance for production settlement/sale fee cash and net PnL; explicit actual zero remains complete while estimated/missing evidence makes affected metrics partial.
- Added explicit-first covered-call attribution, FIFO heuristic downgrade, interval inventory coverage, reservation-based no-double-attribution and mixed-inventory fail-closed behavior.
- Added unsupported inventory status for non-Sell-Put assignment/exercise transitions.
- Preserved canonical later-void restatement at assigned-stock valuation boundaries.

## Changed Files

- `domain/domain/assigned_stock.py`
- `domain/domain/performance/engine.py`
- `src/application/ledger/queries.py`
- `src/application/ledger/api.py`
- `src/application/performance/adapters.py`
- `src/application/performance/service.py`
- `src/application/positions/reporting.py`
- `src/application/positions/workflows.py`
- `src/application/ledger/read_model.py`
- `src/application/agent_tools/operations_impl.py`
- `src/application/agent_tools/materialization_impl.py`
- `src/application/trades/state_reconcile.py`
- `tests/test_assigned_stock_projection.py`
- `tests/test_ledger_assigned_stock_queries.py`
- `tests/test_performance_assignment.py`
- `tests/test_positions_reporting.py`
- `tests/test_performance_engine.py`
- `docs/ASSIGNED_STOCK_RETURN_DESIGN.md`
- `docs/OPTION_PERFORMANCE_DESIGN.md`

## Validation

- S5 focused suite: 53 passed before review fixes.
- S5 plus engine/service compatibility after fixes: 72 passed.
- S1-S4 performance/evidence compatibility: 62 passed.
- Ruff on all touched S5 Python files and tests: passed.
- `git diff --check`: passed.
- Touched-consumer source assertion confirms direct `list_assigned_stock_events` probing exists only in the ledger query boundary.

## Docs Decision

Updated both assigned-stock and option-performance design documents with canonical ownership, principal/fee/PnL formulas, boundary void semantics, historical/current mark rules, covered-call attribution quality and explicit out-of-scope inventory.

## Residual Risks and Uncovered Areas

| Risk | Classification |
|---|---|
| Decimal interval-based capital-days and annualized efficiency remain legacy in the shared projector | covered by later approved S6 |
| Public Agent/CLI exposure and legacy command compatibility | covered by later approved S7 |
| Analysis consumer migration | covered by later approved S8 |
| Portfolio bridge presentation | covered by later approved S9 |
| Shadow reconciliation/cutover and final legacy isolation | covered by later approved S10 |
| Ordinary stock ledger, dividends, tax, split and non-Sell-Put inventory basis | assigned to later work unit; current behavior explicitly fails closed |

No unclassified residual risk remains.
