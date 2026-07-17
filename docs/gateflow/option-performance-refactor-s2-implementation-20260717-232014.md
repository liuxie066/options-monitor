# Gateflow Implementation — Option Performance Refactor S2

- **Gate**: implementation
- **Work unit**: `option-performance-refactor`
- **Slice**: S2 — Canonical Ledger Economic Allocations and Fees
- **Created at**: 2026-07-17 23:20:14 CST（本机时钟）
- **Approved plan**: `docs/gateflow/option-performance-refactor-plan-20260717-224048.md`
- **Plan fix**: `docs/gateflow/option-performance-refactor-plan-fix-20260717-225441.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s2-implementation-20260717-232014.md`
- **Completion status**: implementation-complete；ready-for-code-review

## Objective and Outcome

Made canonical ledger replay the sole owner of option close matching and economic allocation. Valid close-like events now produce stable `OptionEconomicAllocation` facts carrying signed premium cash, gross realized PnL, allocated opening fee, close fee, nullable net PnL, fee quality, strategy attribution and settlement references. Existing `PositionLot` and risk views remain behavior-compatible; new reporting receives allocations only through the legal application ledger API.

## Changed Files

- `domain/domain/ledger/economics.py` — new fee interpretation and canonical allocation builder/model.
- `domain/domain/ledger/projection.py` — emits allocations during the same validated/void-aware lot state transition.
- `domain/domain/ledger/__init__.py` — exports the approved domain allocation and fee surfaces.
- `src/application/ledger/event_codec.py` — imports legacy top-level fee provenance into canonical raw payload and preserves it on application payload conversion.
- `src/application/ledger/queries.py` — exposes `trade_event_economic_allocations(repo)` through canonical replay.
- `src/application/ledger/api.py` — re-exports the legal application boundary.
- `tests/test_ledger_economics.py` — signed short/long economics, partial-close fee conservation, missing/zero/estimated/legacy fees, assignment metadata, void/repair replay and deterministic identity/order.
- `tests/test_ledger_sqlite_workflows.py` — legal API and old canonical event/fee-provenance compatibility assertions.
- `docs/OPTION_PERFORMANCE_DESIGN.md` — records S2 allocation ownership, fee and compatibility contracts.

## Decisions and Invariants

- Projection validates and sorts immutable events, computes the global void set, then applies lot state and economics in one replay path; downstream performance code must not rematch option lots.
- Allocation IDs derive from open event, close event and deterministic sequence. Input order does not affect replay order or identity.
- Short option opening premium is positive cash and closing premium negative; long signs are reversed. Gross realized PnL is the sum of signed premium amounts.
- Assignment/exercise closes option economics only. Stock settlement principal and stock lifecycle economics remain outside S2.
- Opening fees are allocated by original opened quantity; the final close absorbs six-decimal remainder. Close fees stay on the closing allocation.
- Explicit actual zero remains complete; legacy non-zero fees become actual with legacy provenance; zero without provenance is missing; estimated provenance remains estimated.
- Missing opening or closing fee keeps gross PnL observed and sets net PnL to null. Estimated components produce estimated fee quality.
- Existing `PositionLot.realized_pnl` remains compatibility-only and keeps legacy close-fee behavior. Canonical gross/net reporting uses allocations.
- Allocation construction failure produces a projection diagnostic and does not mutate the target lot.

## Validation

```text
python3 -m pytest tests/test_ledger_projection.py tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py -q
81 passed in 1.06s

python3 -m ruff check domain/domain/ledger src/application/ledger tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py
All checks passed!

git diff --check
pass
```

An earlier broader focused run including `tests/test_ledger_event_codec.py` also passed (`84 passed`); the compatibility assertion was subsequently moved into the S2-approved SQLite workflow test file so the final S2 write scope matches the accepted plan.

## Docs Decision

Updated `docs/OPTION_PERFORMANCE_DESIGN.md` because S2 establishes the canonical economic ownership boundary and a downstream-consumed allocation contract. No public Agent/CLI command exists yet.

## Residual Risks and Uncovered Areas

| Area | Classification |
|---|---|
| Period activity/cash/realized aggregation consuming allocations | covered by later approved S3 |
| Historical/current marks and FX evidence | covered by later approved S4 |
| Assigned-stock settlement and stock economics | covered by later approved S5 |
| Public Agent/CLI serialization of allocation-derived metrics | covered by later approved S7 |
| Legacy `PositionLot.realized_pnl` differs from canonical allocation net PnL | fixed contractually in current slice: compatibility field retained and explicitly non-authoritative |
| General multi-lot close matching | fixed in existing ledger writer boundary; canonical stored close events target one lot and projection does not infer across lots |

No unclassified residual risk remains for S2 code review.

## Next Entry Point

S2 code review using `deepreview`.
