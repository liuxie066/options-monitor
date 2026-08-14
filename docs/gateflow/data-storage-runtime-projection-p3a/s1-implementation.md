# Gateflow S1 Implementation — Additive Projection Publication Contract

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S1`
- Gate: `implementation`
- Date: 2026-08-14
- Base: `origin/main@2a7bc5668ecda53d0b0d1c3d8856d976487b5d6f`
- Status: accepted; DeepReview re-review passed with planned later-slice risks

## Outcome

S1 replaces destructive full lot replacement with exact row diff publication while
leaving the full-history projector unchanged. It also establishes durable generation,
head, implementation-identity and trusted-read contracts. Checkpoint selection, tail
replay and runtime fast-path cutover remain disabled and belong to S2-S4.

## Implementation

- Added normalized account sidecars, closed column classifications, source/lot
  generation state, per-account heads, and complete mutation triggers.
- New/populated stores remain startup-bounded: populated tables receive additive
  columns only; account/scalar backfill and normalized index construction are explicit
  migration primitives.
- Added canonical `position_lots_fingerprint.v1` hashing over complete public fields in
  record-id order and an indexed streaming account cursor.
- Added a closed semantic source/import manifest. Its exact implementation digest is
  frozen when the module loads, so transactions perform no source-file reads.
- Added exact lot diff publication with zero DML for unchanged rows. Every existing
  full-oracle publication callsite now uses the shared diff/head boundary.
- Added a distinct projection-publication repository protocol. Projecting transaction
  entrypoints validate this capability before any event or lot DML.
- Added metadata-first trusted reads: version/generation/schema mismatches fail closed
  before retained lot rows are decoded or hashed.
- Added bounded event-id, active-lot and record-id repository queries for later slices;
  they do not change S1 runtime behavior.

Generated dependency/source-inventory artifacts were refreshed because the new
production modules and imports are repository-enforced architecture inputs.

## Correctness evidence

Focused S1 and adjacent ledger/CLI/dependency regression set:

```text
181 passed in 6.55s
```

Static checks:

```text
ruff: all checks passed
compileall: passed
dependency graph: current; production_modules=574; cycles=0
checked-in source inventory: passed
git diff --check: passed
```

Aggregate re-review:

```text
docs/reviews/code-review-20260814-005834.md: pass-with-risks
```

Full repository suite:

```text
2 failed, 4640 passed, 10 skipped in 85.91s
```

The exact HTTP-bind failure passed outside the restricted sandbox (`1 passed in
0.97s`). The other failure reports five pre-existing research-to-ledger imports
and was reproduced unchanged on the untouched `main` worktree.

Coverage includes the mutation/trigger matrix, idempotent event updates, account moves,
zero-lot accounts, unchanged-row zero DML, rollback, cross-account freshness, schema
drift, indexed canonical hashing, populated startup, immutable loaded-source identity,
unsupported repository zero-write behavior, full-writer routing, and close-resolution
regressions.

## Time and space evidence

Final uninstrumented 10,000-row local fixture after the review fixes:

```text
unchanged full publication: 116.293 ms; 10,000 unchanged; zero row DML
single-row changed publication: 119.073 ms; one changed row
populated-store startup: 6.799 ms; 10,000 historical rows untouched
```

Loaded semantic implementation fingerprint, 5 warmups / 30 measured repetitions plus
a separate allocation run:

```text
wall p95: 34.625 ms
CPU p95: 33.494 ms
peak allocation: 3.69 MiB
```

This passes the accepted one-time construction limits of 50 ms wall/CPU and 8 MiB.
The final reference-host acceptance and retained-history scaling gates remain owned by
S5.

## Safety and boundary

- No live SQLite migration, activation, checkpoint creation, or runtime cutover.
- No configuration/service mutation, release/deploy, notification, broker write, data
  deletion, or production data write.
- The original `main` worktree and its user changes remain untouched; implementation is
  isolated in `/private/tmp/options-monitor-data-storage-p3a-20260813`.

## Residual risks

- Full projection still replays O(E): covered by approved S2-S4.
- S1 readiness checks still scan normalized history during a full publication: S3/S4
  must replace this with bounded durable readiness before any fast path can activate.
- Diff construction and successful trusted-read materialization remain O(L) memory;
  retained-lot scaling and the merge-cursor decision are owned by S5 measurement.
- Global source-generation fanout is O(A), and flat changed-account fingerprinting is
  O(L_account): both are explicit S5 performance gates.
- Populated stores remain untrusted until explicit migration and verification in S5.
- Five pre-existing research-to-ledger import-boundary offenders are present on the
  untouched `main` baseline; they are existing architecture debt, not introduced by S1.
