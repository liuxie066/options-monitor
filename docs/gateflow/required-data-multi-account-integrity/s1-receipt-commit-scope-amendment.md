# Gateflow S1 Receipt-Commit Scope Amendment

- Gate: plan amendment before S1 implementation completion
- Work unit: `required-data-multi-account-integrity`
- Slice: S1 — Required-data completion, receipt, seal, and gateway truth
- Artifact path: `docs/gateflow/required-data-multi-account-integrity/s1-receipt-commit-scope-amendment.md`
- Status: accepted (`pass-with-risks`)
- PlanReview artifact: `docs/reviews/plan-review-20260804-114152.md`

## Evidence

The required-data quote publisher validates freshness before calling
`publish_source_receipt()`. The generic publisher then writes the immutable
payload, constructs and validates the receipt, and finally commits the receipt.
Without an owner-level check between those last two writes, an observation can
cross its TTL after the earlier quote check but still acquire a completion
receipt.

The write-once receipt boundary is owned by
`src/application/position_advice_source_receipts.py`, which was not in the
original S1 allowlist. A quote-only precheck cannot close this payload-first /
receipt-last time-of-check-to-time-of-use window.

## Exact scope addition

- Add `src/application/position_advice_source_receipts.py` and
  `tests/test_position_advice_source_producers.py` to S1.
- Add one optional keyword-only `before_receipt_commit` callback to
  `publish_source_receipt()`.
- Invoke the callback after payload publication and after the receipt has been
  fully constructed, internally validated, and serialized, but immediately
  before the write-once receipt commit.
- Pass a copy of the completed receipt mapping to the callback. Callback
  failure propagates and leaves at most an orphan immutable payload; it must
  never leave a completion receipt.
- Required-data quote publication supplies a callback that revalidates the raw
  aggregate and every child request against a fresh wall clock in production.
  Explicit injected `now` remains deterministic for tests.
- Do not change receipt schema, hash inputs, path layout, validation rules,
  existing producer behavior, retries, or recovery semantics. Other producers
  omit the optional callback and remain unchanged.

## Success signal

- Existing callers that omit the callback behave byte-for-byte as before.
- A successful callback sees a complete receipt exactly once before commit.
- A rejected callback may leave one orphan payload but leaves zero receipts.
- Required-data that crosses TTL during payload publication cannot acquire a
  quote receipt; exact same-run recovery may later ignore the orphan and retry.

## Validation

- Generic publisher tests prove payload-first ordering, complete receipt input,
  exactly-once callback execution, exception propagation, and zero receipt on
  rejection.
- Required-data quote tests prove the callback is wired at the owner boundary
  and a simulated TTL crossing leaves an orphan payload with zero receipt.
- Existing source-producer and quote receipt suites prove optional-call-site
  compatibility and receipt-last re-entry behavior.

## Residual risk

- This amendment closes quote freshness at local receipt commit. It does not
  redesign timestamps, make payload and receipt a filesystem transaction, or
  add a distributed lock.
- Orphan payload cleanup remains unnecessary for correctness because only a
  valid receipt grants authority; cleanup policy remains outside S1.
- Implementation acceptance must prove the production clock is sampled again
  after payload publication, with the precheck fresh and the commit-boundary
  sample stale. Callback-ordering evidence alone is insufficient.
