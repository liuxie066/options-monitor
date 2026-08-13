# Gateflow S3 Implementation — Checkpoint Store and Strict-Tail Runtime

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S3`
- Gate: `implementation`
- Date: 2026-08-14
- Base: `S2@d355193a`
- Status: accepted after DeepReview fix and re-review
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p3a/s3-implementation.md`

## Outcome

S3 adds a bounded, disposable SQLite checkpoint cache and one internal
transactional runtime for forced-full or fast-if-safe projection. External/public
writers remain on their existing full-oracle path; checkpoint mode defaults to
disabled and no live store was migrated or activated.

## Implementation

- Added the canonical checkpoint envelope, exact length-framed event-prefix hash,
  schema/implementation/source bindings, strict decoding and 64 MiB payload cap.
- Added strict ordered-tail reads and the supporting `(trade_time_ms, event_id)`
  index contract without startup index builds on populated stores.
- Extended event triggers with O(K) checkpoint invalidation for prefix
  intersections, controls, updates, deletes and unknown event types.
- Added one `BEGIN IMMEDIATE` runtime that atomically writes events, only touched
  lot rows, all account heads, optional checkpoint rotation and bounded pruning.
- Added collision guards for tail-open ids across checkpoint state, the suffix and
  retained physical rows, with canonical full replay as the unsafe-tail oracle.
- Ordinary invalidation performs one full fallback and seeds a replacement
  checkpoint when mode is enabled. Corruption/schema/implementation/capacity
  failure leaves exact lots/heads possible while checkpoint mode remains
  untrusted.
- Added 100-event / 1-MiB rotation, at most three retained rows and zero
  checkpoint writes between rotations.
- Kept the loaded projector fingerprint process-frozen; ordinary runtime
  transactions perform no source-file reads or hashes.

## Validation

Focused S3 plus adjacent ledger/publication set:

```text
175 passed in 7.36s
ruff: passed
compileall: passed
dependency graph: current; production_modules=576; cycles=0
semantic implementation digest: ab13f4eba480beaccbb728796601885575494e92f3803ab6ad3902144e2c2547
git diff --check: passed
```

Full repository suite:

```text
2 failed, 4691 passed, 10 skipped, 5 warnings in 101.85s
```

The HTTP bind failure is sandbox-only and passed outside the restricted sandbox
(`1 passed in 0.88s`). The other failure is the same five pre-existing
research-to-ledger imports recorded in the accepted S1/S2 baseline; S3 adds no
non-ledger import of the new runtime.

## Time and space evidence

Deterministic local checkpoint fixture:

```text
active lots: 4,000
checkpoint payload: 6,936,390 bytes
fast decode/tail peak allocation: 63,612,120 bytes
contract limit: 67,108,864 bytes
result: pass
```

The focused suite also proves zero full-prefix calls/hashes, one bounded newest
checkpoint payload read, no global decoded lot-list read, no source reads after
module initialization, zero checkpoint writes between rotations and K<=3.

## Docs decision

Generated dependency documentation was refreshed for the new production module.
Public/operator documentation is intentionally deferred to approved S5 because
no public command, config or activation surface exists in S3.

## Safety boundary

- No public writer cutover, migration, activation, release, deployment, live
  SQLite/config/service mutation, notification, broker write or deletion.
- Checkpoint rows are disposable and never become trading authority.
- The original dirty `main` worktree remains untouched; all changes are in the
  isolated worktree.

## Residual risks

- Public facade routing, mixed-batch primary-key preflight and shadow parity are
  covered by later approved slice S4.
- Explicit migration/readiness/activation, reference-host latency/CPU/WAL/
  allocation acceptance and operator docs are covered by later approved slice S5.
- Full replay remains O(E) by design and is the canonical fallback/oracle.
- The five pre-existing research-to-ledger imports are assigned to their existing
  architecture-boundary work, not this slice.
