# Gateflow S1 Review Fix

- Gate: S1 fix
- Work unit: `lifecycle-receipt-batching`
- Input review: `docs/reviews/code-review-20260802-053512.md`
- Status: accepted findings fixed; pending re-review

## DR-S1-01 — accepted — fixed

- `claim_next_notification_batch()` now acquires only the lease.
- `attempt_count` increments atomically at `claimed -> send_started`, the first durable proof that provider I/O may begin.
- Three consecutive stale claims keep attempt count at zero; a later send-started becomes attempt one.
- Added a deterministic lease-recovery regression.

## DR-S1-02 — accepted — fixed

- Repository insertion now validates batch/schema/route self-consistency.
- Frozen member IDs must exactly match the ordered binding input.
- Every envelope is compared with the stored outbox row's immutable identity, revision, timestamps, payload hash and payload.
- Frozen first/last timestamps must equal the bound rows.
- Tampered member ID, payload hash and payload tests all prove full rollback with no batch or binding.

## Validation

- Focused S1 suite after fixes: `40 passed`.
- Ruff: pass.
- `git diff --check`: pass.
