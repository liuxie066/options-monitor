# Gateflow Fix — Option Performance Refactor S2

- **Gate**: code review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S2
- **Created at**: 2026-07-17 23:30:55 CST（本机时钟）
- **Review source**: `docs/reviews/code-review-20260717-232342.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s2-fix-20260717-233055.md`
- **Completion status**: fix-complete；ready-for-re-review

## Finding Decisions and Fixes

### CR-S2-01 — Multi-lot close fee duplication

- **Decision**: accepted.
- **Fix**: added a shared Decimal close-fee splitter used by both writer close-target paths. It allocates by matched contracts, lets the final segment absorb the six-decimal remainder, updates each stored event `fees`, and writes the allocated amount into valid actual/estimated provenance.
- **Validation**: public writer test closes two FIFO lots with one 2 USD fee event and proves two 1 USD allocations, conserved total fee and 198 USD aggregate realized net.
- **Status**: 已修复.

### CR-S2-02 — Economic failure blocked risk close

- **Decision**: accepted.
- **Fix**: lot state transition now executes after the economics attempt regardless of allocation success. Failed economics emit diagnostics and produce no allocation. Opening-fee allocation state advances independently so a later valid close does not absorb the failed segment's fee share.
- **Validation**: a non-finite close price yields `economic_allocation_failed`, no allocation for that close, a closed lot, and the later close receives only its 0.5 opening-fee share.
- **Status**: 已修复.

### CR-S2-03 — Currency/multiplier mismatch

- **Decision**: accepted.
- **Fix**: projection compares normalized lot/event currency and Decimal multiplier before constructing economics. Mismatch emits `target_economic_units_mismatch`, produces no allocation, advances fee state, and preserves the existing lot close transition.
- **Validation**: separate currency and multiplier mismatch cases assert closed risk state, absent economics and explicit diagnostics.
- **Status**: 已修复.

### CR-S2-04 — Estimated fee entered production realized net

- **Decision**: accepted.
- **Fix**: `realized_pnl_net` is now computed only when allocated opening fee and close fee are both `actual`. Estimated and missing fees preserve gross and evidence/quality but return null production net.
- **Validation**: estimated close fee remains visible as `0.4`, quality remains `estimated`, gross remains `100`, and net is null.
- **Status**: 已修复.

### CR-S2-05 — Invalid provenance silently inferred as legacy actual

- **Decision**: accepted.
- **Fix**: explicit invalid basis or invalid amount now returns a missing `FeeFact` with a reason. Invalid legacy numeric fees also fail closed as missing. These metadata errors no longer claim actual legacy provenance and do not prevent lot state projection.
- **Validation**: misspelled `actaul` with non-zero fees returns missing/null and an invalid-basis reason.
- **Status**: 已修复.

## Additional Contract Update

`docs/OPTION_PERFORMANCE_DESIGN.md` now records multi-lot fee conservation, actual-only production net, malformed provenance fail-closed semantics, economic-unit validation and the separation between economic completeness and risk state transition.

## Validation

```text
python3 -m pytest tests/test_ledger_projection.py tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py -q
86 passed in 0.99s

python3 -m ruff check domain/domain/ledger src/application/ledger tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py
All checks passed!

git diff --check
pass
```

## Residual Risks and Uncovered Areas

| Area | Classification |
|---|---|
| S3 must surface effective closes with missing allocations as incomplete rather than silently omit them | covered by later approved S3 |
| Historical evidence, assigned stock and capital | covered by later approved S4-S6 |
| Legacy `PositionLot.realized_pnl` may contain non-finite/wrong-unit arithmetic for malformed events while risk quantity state is preserved | assigned to later work unit; canonical performance refuses allocation and diagnostics expose the malformed event; S2 does not rewrite the compatibility field |

No unclassified residual risk remains.

## Next Entry Point

S2 code re-review using `deepreview`.
