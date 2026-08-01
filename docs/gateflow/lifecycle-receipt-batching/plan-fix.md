# Gateflow Plan Fix

- Gate: plan fix
- Work unit: `lifecycle-receipt-batching`
- Input review: `docs/reviews/plan-review-20260802-052141.md`
- Status: all accepted findings repaired; pending re-review

## PR-01 — accepted — fixed

- Bound non-terminal members now use `status=batched`, which v1.8.5 does not claim.
- Batch owns pending/claimed/send-started/retryable-failure states.
- Only frozen terminal outcomes project to members; exhausted failure also writes `attempt_count=3`.
- Plan requires a rollback-compatibility regression using the old claim predicate.

## PR-02 — accepted — fixed

- The process-level dispatcher no longer holds `process_lock` during provider I/O.
- Planner/claim, send-started and completion use separate short SQLite transactions.
- Plan requires a blocking-sender concurrency regression proving unrelated ledger work can proceed.

## PR-03 — accepted — fixed

- The plan now defines exact evidence classes for confirmed, accepted, explicit pre-acceptance failure and unknown.
- 4xx/provider rejection with unambiguous response may retry; timeout/transient/fallback ambiguity always freezes unknown.
- Classifier evidence is preserved in the batch provider receipt and tested explicitly.

## Additional clarification

- The account allow-set is derived from `source.account` and normalized `account_mapping` values using existing source/global receipt precedence.
- `batch_id` is fixed to a provider-safe `tlb_` plus 32 lowercase hex characters.
