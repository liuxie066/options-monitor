# Gateflow Implementation — Option Performance Refactor S3

- **Gate**: implementation
- **Work unit**: `option-performance-refactor`
- **Slice**: S3 — Core Period Performance Engine: Activity, Cash, Realized PnL
- **Created at**: 2026-07-17 23:48:46 CST（本机时钟）
- **Approved plan**: `docs/gateflow/option-performance-refactor-plan-20260717-224048.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s3-implementation-20260717-234846.md`
- **Completion status**: implementation-complete；ready-for-code-review

## Objective and Outcome

Implemented the pure native-currency period engine and read-only application service for option activity, direct cash movements and realized option PnL. The engine consumes effective canonical events plus S2 allocations, never matches lots, assigns realized PnL to close time, separates assignment/exercise stock settlement principal from option PnL, and produces the same metric reduction across period/month/account/symbol views.

## Changed Files

- `domain/domain/performance/engine.py` — fact model, effective-event selection, signed option/settlement cash, fee completeness, realized allocation reduction, quality and breakdowns.
- `src/application/performance/__init__.py` — application service export.
- `src/application/performance/adapters.py` — legal ledger-API loader, canonical application-payload decoder and scoped diagnostic metadata.
- `src/application/performance/service.py` — period normalization, account/broker normalization and read-only service facade.
- `tests/test_performance_engine.py` — short/long signs, close-period PnL, assignment principal, missing fees, voids, missing allocation, breakdowns, scoped diagnostics and malformed settlement.
- `tests/test_performance_service.py` — SQLite legal-API path, scope filtering, row suppression and no-write assertion.
- `docs/OPTION_PERFORMANCE_DESIGN.md` — authoritative S3 cash/PnL ownership and completeness contract.

## Decisions and Invariants

- Effective canonical events own activity and direct cash; `OptionEconomicAllocation` alone owns realized option PnL.
- Option premium activity is never added to PnL. A period containing only an open has premium/cash but no realized PnL.
- Short open/close cash signs are positive/negative; long signs are negative/positive.
- Assignment/exercise stock principal is a separate signed cash fact. It never reduces option realized PnL.
- Actual fee facts create negative fee cash and complete production net. Estimated/missing fees preserve gross and make affected net currency null/partial.
- Stock settlement fees must be explicit for complete total net cash; malformed stock settlement fails closed while option realized remains available.
- An effective close without an allocation counts contracts/direct cash but creates explicit missing realized gross/net facts.
- A metric never publishes a partial subtotal for a currency: any missing constituent removes that currency from the envelope and marks it partial.
- Diagnostics are filtered by selected period/account/broker, including decode errors whose event could not enter the effective event list.
- Service imports only `src.application.ledger.api`, performs no persistence and emits internal `option_period_performance.core.v1`; public Agent/CLI is deferred to S7.

## Validation

```text
python3 -m pytest tests/test_performance_engine.py tests/test_performance_service.py -q
10 passed

python3 -m pytest tests/test_performance_engine.py tests/test_performance_service.py tests/test_ledger_economics.py -q
23 passed in 0.49s

python3 -m ruff check domain/domain/performance src/application/performance tests/test_performance_engine.py tests/test_performance_service.py
All checks passed!

git diff --check
pass
```

## Docs Decision

Updated `docs/OPTION_PERFORMANCE_DESIGN.md` because S3 defines the canonical distinction between activity, cash and realized PnL and establishes the internal service response consumed by later slices.

## Residual Risks and Uncovered Areas

| Area | Classification |
|---|---|
| Opening/ending unrealized and period-total PnL | covered by later approved S4 valuation evidence |
| FX/CNY translation | covered by later approved S4 |
| Assigned-stock realized/unrealized lifecycle beyond direct settlement cash | covered by later approved S5 |
| Capital exposure/efficiency | covered by later approved S6 |
| Public Agent/CLI parser, row cap and aggregate configured-account union | covered by later approved S7 |
| Effective close without allocation requires public incomplete explanation | fixed in core fact/quality contract; public rendering covered by S7 |

No unclassified residual risk remains for S3 review.

## Next Entry Point

S3 code review using `deepreview`.
