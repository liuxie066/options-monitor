# Gateflow Plan Fix — Option Performance Refactor

- **Gate**: plan review fix
- **Work unit**: `option-performance-refactor`
- **Created at**: 2026-07-17 22:55:33 CST（本机时钟）
- **Reviewed plan**: `docs/gateflow/option-performance-refactor-plan-20260717-224048.md`
- **Finding source**: `docs/reviews/plan-review-20260717-224551.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-plan-fix-20260717-225441.md`
- **Decision**: accept and fix PR2-01 through PR2-06

## Accepted Findings and Exact Plan Changes

### PR2-01 — Instrument identity

- **Decision**: accepted。
- **Plan fix**: defined `OptionInstrumentKey` and `StockInstrumentKey`, deterministic `option:v1`/`stock:v1` codecs, Decimal normalization, explicit `ContractKey` conversion and structured schema identity columns。
- **Validation added**: round-trip/stability/invalid-decode/cross-account-and-side reuse tests in S1/S4。
- **Status**: 已修复。

### PR2-02 — Live marks and evidence capture loop

- **Decision**: accepted。
- **Plan fix**: added current-only read-through collection using existing OpenD snapshot/chain/spot and no-write FX adapters; specified exact option-code resolution, batch fetch, midpoint/last fallback, timestamp and diagnostic rules; added explicit dry-run-by-default evidence capture sharing the import transaction。
- **Validation added**: current fetch without writes, capture dry-run/apply and later historical replay selecting captured fact IDs。
- **Status**: 已修复。

### PR2-03 — Assigned-stock legal ledger API

- **Decision**: accepted。
- **Plan fix**: S5 now adds and exports `assigned_stock_event_log(repo)` in `src/application/ledger/queries.py` and `api.py`; performance and touched legacy consumers must use that boundary instead of direct repository introspection。
- **Validation added**: focused ledger API tests and touched-path search assertion。
- **Status**: 已修复。

### PR2-04 — Public contract and consumer ownership

- **Decision**: accepted。
- **Plan fix**: specified exact Agent defaults, conditional period fields, config/data-config resolution, account aggregate semantics, broker normalization, row cap and current/historical refresh behavior; specified import/capture CLI contracts; explicitly listed assistant/Copilot/CLI consumers and all close-advice tests。
- **Validation added**: Agent/CLI parity and exact consumer tests。
- **Status**: 已修复。

### PR2-05 — Capital-days integration

- **Decision**: accepted。
- **Plan fix**: replaced ambiguous active-day math with `capital_days = notional * overlap_ms / 86_400_000`; defined `[open,close)` state intervals, partial-close transition and assignment handoff without overlap/gap。
- **Validation added**: same-day, intraday partial-close, midnight, cross-period and assignment-transition cases。
- **Status**: 已修复。

### PR2-06 — Evidence migration/import state machine

- **Decision**: accepted。
- **Plan fix**: explicit migration state machine and no-DDL read behavior; fixed v1 envelope; complete dry-run validation; migration plus batch in one transaction; idempotency, correction identity, target existence and cycle rules; all conflicts roll back the whole batch。
- **Validation added**: missing-schema no-mutation, idempotent migration, whole-batch rollback and correction-chain tests。
- **Status**: 已修复。

## Validation

- Plan headings and slice ordering remain S1-S10 and Gateflow-compatible。
- Plan now contains direct file ownership and exact validation commands for every accepted finding。
- Search confirmed all six finding concepts are present in contracts and implementation slices。
- No implementation source code was changed in this fix gate。

## Residual Risks and Uncovered Areas

| Risk | Classification |
|---|---|
| Historical evidence starts sparse even with capture/import | covered by later approved S4 quality semantics and operational backfill；not a plan blocker |
| General stock inventory outside supported Sell Put lifecycle | assigned to later work unit；explicit incomplete output remains current contract |
| External opening/ending cash facts may be absent | covered by later approved S9 structured unavailable semantics |
| Existing OpenD adapters may lack one reusable seam for exact timestamps/code resolution | covered by later approved S4 allowed conditional adapter modification plus focused tests |

No unclassified residual risk remains。

## Completion Status

- **Plan fix**: pass
- **Blocking open questions**: none
- **Current gate / next entry point**: plan re-review using `planreview`
