# Gateflow Fix — Sell Put Top1 W3 Current-Changes DeepReview

- Gate: `fix`
- Work unit: `sell-put-top1-w3`
- Review artifact: `docs/reviews/code-review-20260815-124143.md`
- Status: finding addressed; pending Kimi re-review

## Finding 1 — accepted in part, fixed; speculative claim-order change rejected

Accepted issue: after a validation proposal invalidates authorization, the lifecycle already has enough current-state evidence to reject a stale hash before publishing the new content-addressed commitment. `start_validation()` now performs a read-side phase and exact-authorization preflight before any filesystem write. The store transaction remains the final authority and repeats every check to close races.

Regression coverage now relocks a changed 20-date proposal, attempts start with the stale prior hash, and proves `authorization_required`, no new commitment file, and no `validation_started` business event.

The suggested move of `_claim_event()` after all store checks is rejected: `_claim_event()` and the checks execute inside one `BEGIN IMMEDIATE` transaction, so every failed check rolls the event back before any reader can observe it. Claim-first is also required for a second caller idempotency key to replay the already-committed natural start fact after the state has advanced; moving it behind current-state guards would turn that legal replay into `invalid_transition`. No external event subscriber exists in W3, and adding one would require a separate contract change.

## Verification

- `tests/test_strategy_lab_top1_store.py` plus architecture guard: `13 passed`.
- Ruff: pass.
- BasedPyright error level: `0 errors, 0 warnings, 0 notes`.
