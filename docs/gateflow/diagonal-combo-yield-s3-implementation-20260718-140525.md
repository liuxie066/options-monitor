# Diagonal Combo Yield — Slice S3 Implementation

## Gate State

- Slice: S3 — Assignment lineage and residual Call classification
- Decision: accepted after mandatory deepreview
- Next gate: accepted slice commit, then S4 implementation

## Implemented Scope

- Lifecycle assignment, exercise, and expiry-close events now copy canonical Combo strategy metadata from the target option lot into the immutable event payload.
- Assigned-stock lots and sale read rows preserve `strategy`, `leg_role`, `strategy_group_id`, `strategy_snapshot`, and `expiry_structure` when present.
- Added pure domain `build_full_group_lifecycle` synthesis over option inventory, active assignment events, assigned-stock lots, and stock-sale state.
- Added reporting-only `full_group_lifecycle`; Close Advice is not involved.
- Added quantity reconciliation for partial assignment: `put_open + assigned_contracts == call_open` identifies the truly residual Call quantity without predicting value.
- Added explicit classifications: `residual_call`, `assigned_stock_with_residual_call`, `assigned_stock_only`, `closed`, and `review_required`.
- Missing settlement, mixed active-combo/assigned-stock states, invalid quantities, and identity conflicts fail closed with explicit lifecycle issues.
- Assigned-stock sale changes remaining shares but does not erase assignment/group history.

## Preserved Boundaries

- No automatic assigned-stock sale.
- No automatic Call exercise.
- No assignment inference from option close markers alone.
- No new mutable group table or database migration.
- No production configuration, notification, or broker-facing writes.

## Validation

```text
python3 -m pytest \
  tests/test_positions_reporting.py \
  tests/test_option_positions_cli.py \
  tests/test_option_positions_domain.py \
  tests/test_combo_yield_lifecycle.py

107 passed
```

```text
git diff --check
passed
```

## Covered Scenarios

- Put normal close + Call open → residual Call.
- Put expiry + Call open → residual Call.
- Full assignment + Call open → assigned stock with residual Call.
- Partial assignment + Put remainder + Call → quantity-reconciled assigned stock with residual Call.
- Assigned stock with no open options → assigned stock only.
- Assigned stock sold + all options closed → closed while preserving history.
- Missing assignment settlement → review required.
- Assignment and expiry-close event payloads retain group/snapshot metadata.

## Review Decision

- Review: `docs/reviews/code-review-20260718-140629.md`
- Decision: pass; no material findings.
- Residual boundary: historical assignment events without explicit group metadata remain unlinked rather than guessed.
