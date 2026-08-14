# Gateflow S1 DeepReview Fix

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S1`
- Gate: `fix`
- Initial review: `docs/reviews/code-review-20260814-000656.md`
- Status: accepted; aggregate re-review passed with planned later-slice risks

## Finding decisions

### S1-DR-01 — accepted — fixed

The semantic implementation result is now computed at module import and frozen for the
process. Publication and trusted-read transactions consume only the frozen value. A
regression makes every later `Path.read_bytes()` fail and proves publication still
succeeds without transaction-time source I/O.

### S1-DR-02 — accepted — fixed

Account discovery reads only durable heads and unions the current diff's known/touched
accounts. Trade-event inserts/semantic updates create zero-lot account heads, so later
publication never scans transaction history for account discovery. SQL tracing freezes
this boundary.

### S1-DR-03 — accepted — fixed

Trusted reads now fetch only source/head/schema metadata first in one read transaction.
They scan/hash account lots only after every cheap eligibility check passes. A generation
mismatch regression replaces the snapshot reader with an exception and proves zero lot
scan on rejection.

### S1-DR-04 — accepted — fixed

Added the purpose-specific `(account, record_id)` index while preserving the existing
expiration query index. `EXPLAIN QUERY PLAN` coverage rejects any fingerprint query that
uses a temporary B-tree. The post-fix 10,000-row publication remains well inside the
future combined 500-ms gate.

### S1-DR-05 — accepted — fixed

Added `PositionProjectionPublicationRepo` and removed the legacy publication fallback.
All projecting transaction entrypoints request the new contract before opening a
transaction or invoking their callback. Regressions prove unsupported repositories
perform neither event nor lot writes.

### S1-DR-06 — rejected with evidence — no code change

The proposed restoration of `ORDER BY updated_at_ms DESC, record_id DESC` was tested and
rejected because the previous full replacement assigned the same publication timestamp
to every row. Its observed stable tie-break was therefore `record_id DESC`; downstream
close selection depended on that behavior. Under exact diff, restoring updated-time
priority changed actual public selection and failed:

- `test_resolve_trade_close_apply_persists_per_lot_target_events`
- `test_multi_lot_broker_close_rolls_back_every_split_when_second_write_fails`

Keeping `record_id DESC` preserves the behavior the old full replacement actually
exposed while allowing unchanged rows to retain timestamps. Treating the old nominal
first sort key as a contract would introduce, rather than prevent, the S1 regression.

## Verification

```text
focused and adjacent tests: 181 passed
ruff: passed
compileall: passed
dependency graph/source inventory: passed
git diff --check: passed
```

Performance evidence and all classified residual risks are recorded in
`s1-implementation.md`.

Aggregate re-review evidence:

```text
docs/reviews/code-review-20260814-005834.md: pass-with-risks
```

## Scope discipline

During fix verification an automatic formatter rewrote existing large files. Those
non-semantic edits were removed; the remaining writer changes are limited to publication
routing and pre-DML contract validation. No live or production action was performed.
