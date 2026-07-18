# Diagonal Combo Yield — Slice S2 Implementation

## Gate State

- Slice: S2 — Durable diagonal intake identity and compositional inventory
- Decision: accepted after deepreview fix/re-review
- Next gate: accepted slice commit, then S3 implementation

## Implemented Scope

- Added domain-owned stable Combo Yield pair fingerprint and account-scoped group-ID helpers.
- Preserved explicit diagonal `strategy_group_id` and `expiry_structure` through trade preview, persisted event payload, projection, and repository restart.
- Added fail-closed diagonal intake validation for missing/conflicting group metadata, conflicting expiry structure, invalid expiry ordering, malformed quantity evidence, over-coverage, and ambiguous leg expiries.
- Kept same-expiry inference behavior on its existing path.
- Made diagonal partial fills quantity-aware: multiple same-expiry lots are aggregated, progressive fills are allowed up to the opposite-leg total, and singular paired-record links are omitted when the companion is composite.
- Added pure domain option-group inventory projection with explicit quantities, classifications, evidence scope, and data-quality issues.
- Added read-only `combo_yield_groups` to account position context.

## Explicit Non-Goals Preserved

- No assignment-stock inference or propagation in S2.
- No Close Advice action changes.
- No database migration or mutable Combo group table.
- No production configuration, notification, or broker-facing writes.

## Validation

```text
python3 -m pytest \
  tests/test_trades_resolver_open.py \
  tests/test_positions_context_builder_partial_close.py \
  tests/test_combo_yield_lifecycle.py \
  tests/test_sell_put_linked_call_helper.py

103 passed (S2 focused suite including manual-open CLI)
```

Ledger compatibility validation:

```text
python3 -m pytest \
  tests/test_ledger_publisher.py \
  tests/test_ledger_service.py \
  tests/test_ledger_sqlite_workflows.py

70 passed
```

Additional checks:

```text
python3 -m py_compile src/application/trades/resolver.py
passed

git diff --check
passed
```

## Evidence Highlights

- Put-first and Call-first diagonal intake both preserve explicit identity.
- SQLite repository restart reconstructs the companion from persisted lot metadata.
- Two partial Call lots totaling one Put quantity are accepted without choosing an arbitrary paired record.
- Progressive partial fills are accepted while over-coverage fails closed.
- Inventory aggregates multiple lots and classifies quantity mismatch, missing identity, malformed quantity, missing expiry, missing Call, and residual Call states deterministically.

## Review Decision

- Initial review: `docs/reviews/code-review-20260718-134637.md`
  - Accepted finding DR-S2-001: manual/structured open persisted only nested snapshot, so projected top-level group identity and context inventory could disappear.
- Fix: promote existing strategy metadata keys through preview/event/projection; expose minimal CLI snapshot input; document the public argument; add restart reconstruction coverage.
- Re-review: `docs/reviews/code-review-20260718-135205.md`
  - Decision: pass; no unresolved material findings.
