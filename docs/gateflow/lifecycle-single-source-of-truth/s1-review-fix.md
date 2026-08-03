# Gateflow S1 Review Fix — Canonical Lifecycle State Ownership

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `code review -> fix`
- Slice: `S1`
- Date: 2026-08-04
- Review artifact: `docs/reviews/code-review-20260804-002940.md`
- Status: fixed and re-reviewed
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/s1-review-fix.md`

## Finding decision

### S1-01 — accepted — 已修复

The no-effective-pairing branch now applies the canonical reason-state allowlist before invoking any reconciliation helper.

- Allowed: `cause_pending`, `partially_resolved`, `needs_review`.
- Skipped: `resolved`, `conflict`, and any unrecognized state.
- The effective-pairing branch reuses the same normalized `reason_state` value, preserving its existing allowlist.

## Regression coverage

Added a conflict/no-effective-pairing deadline test that makes both the close-reason resolver and provider collector raise if called. The due result must contain zero cases and zero results.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s1_rereview python3.12 -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py
```

Result: `26 passed in 1.57s`.

`git diff --check`: pass.

## Docs decision

No operator documentation change is needed for this internal absorbing-state guard.

## Residual risks

- S2 account isolation remains covered by the later approved slice.
- Aggregate validation remains required.

## Completion status

Finding S1-01 is fixed; accepted re-review artifact: `docs/reviews/code-review-20260804-003133.md`.
