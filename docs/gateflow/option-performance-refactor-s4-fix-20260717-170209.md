# Gateflow S4 Fix

## Gate

- Work unit: `option-performance-refactor`
- Slice: `S4 — Deterministic Valuation and FX Evidence`
- Gate: code review fix
- Base accepted slice: `ecd7c306` (S3)
- Input review: `docs/reviews/code-review-20260717-164622.md`

## Scope

This fix pass addresses all five accepted S4 review findings without expanding into S5-S10:

- external evidence canonicalization;
- persisted/live identity conflict handling;
- unsupported evidence schema quality propagation;
- stored exact market-code promotion during cross-account dedupe;
- compact report provenance while preserving full capture envelopes.

## Findings and Fixes

| Finding | Decision | Fix | Verification |
|---|---|---|---|
| `S4-CR-01` | accepted | Added recursive JSON-safe external raw normalization; converted datetime/Timestamp, Decimal, NumPy-like scalars, and non-finite floats; canonical JSON now rejects NaN with `allow_nan=False`. | External raw/canonical-evidence regression tests pass. |
| `S4-CR-02` | accepted | Validate live facts against persisted facts before merging; on any source-identity conflict discard the complete live batch, retain persisted evidence, and add a scoped diagnostic. | Current merge conflict regression test passes. |
| `S4-CR-03` | accepted | Convert `unsupported_schema` repository state into a valuation diagnostic so top-level quality becomes partial even with no open position. | Unsupported-schema/no-position quality test passes. |
| `S4-CR-04` | accepted | During account-independent option dedupe, promote a later representative that has a stored exact market code when the existing representative has none; conflicting stored codes still fail closed. | No-code then stored-code ordering test passes. |
| `S4-CR-05` | accepted | `CurrentEvidenceCollection.to_dict()` now emits only status, counts, diagnostics, and fact IDs; the full evidence envelope remains available only through `.envelope` and explicit capture. | Compact provenance/no-raw regression test passes. |

## Changed Files

- `domain/domain/performance/models.py`
- `src/application/performance/evidence_collection.py`
- `src/application/performance/service.py`
- `src/infrastructure/performance_evidence_sqlite.py`
- `tests/test_performance_evidence_collection.py`
- `tests/test_performance_evidence_sqlite.py`
- `tests/test_performance_service.py`

The S4 implementation also includes the previously recorded evidence, valuation, adapter, and OpenD timestamp-seam changes in `docs/gateflow/option-performance-refactor-s4-implementation-20260717.md`.

## Validation

Passed after the final test database-path correction:

```text
python3 -m pytest tests/test_performance_evidence_sqlite.py tests/test_performance_evidence_collection.py tests/test_performance_valuation.py tests/test_performance_engine.py tests/test_performance_service.py -q
# 39 passed

python3 -m pytest tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py tests/test_performance_engine.py tests/test_performance_evidence_sqlite.py tests/test_performance_evidence_collection.py tests/test_performance_valuation.py tests/test_performance_service.py tests/test_ledger_projection.py tests/test_ledger_economics.py tests/test_ledger_event_codec.py tests/test_ledger_service.py tests/test_ledger_sqlite_workflows.py -q
# 174 passed

python3 -m ruff check domain/domain/performance src/application/performance src/infrastructure/performance_evidence_sqlite.py src/application/opend_market_snapshot_fetching.py tests/test_performance_evidence_sqlite.py tests/test_performance_evidence_collection.py tests/test_performance_valuation.py tests/test_performance_engine.py tests/test_performance_service.py
# passed

git diff --check
# passed
```

## Docs Decision

No public CLI or Agent contract is introduced in S4. `docs/OPTION_PERFORMANCE_DESIGN.md` remains the authoritative S4 evidence/valuation design and already reflects these invariants.

## Residual Risks and Uncovered Areas

| Risk / uncovered area | Classification |
|---|---|
| Assigned-stock lifecycle and valuation are not yet implemented | covered by later approved S5 |
| Capital exposure and efficiency are not yet implemented | covered by later approved S6 |
| Public Agent/CLI capture/import/report surfaces are not yet wired | covered by later approved S7 |
| Analysis consumers are not yet migrated | covered by later approved S8 |
| Portfolio PnL/cash bridges are not yet implemented | covered by later approved S9 |
| Historical evidence remains sparse until capture/import/backfill workflows exist | covered by later approved S7/S10; current quality remains explicit |
| Real OpenD field variability is not exercised in this slice | assigned to operational validation in S10; adapter normalization and diagnostics fail closed |
| General non-assignment stock inventory | assigned to a later work unit and remains explicitly outside this work unit |

No unclassified residual risk remains.

## Completion Status

- Fix gate: complete
- Next entry point: S4 re-review using `deepreview`
- Artifact path: `docs/gateflow/option-performance-refactor-s4-fix-20260717-170209.md`
