# Gateflow Goal Confirmation — data-storage-runtime-projection-p3a

- Gate: `goal confirmation`
- Work unit: `data-storage-runtime-projection-p3a`
- Base: `origin/main@f25352f10ef8f48e87f4c2be98a3765d292b8bc3`
- Plan refresh base:
  `origin/main@2a7bc5668ecda53d0b0d1c3d8856d976487b5d6f`; the intervening
  commits contain release/dependency-document metadata only and do not change
  the accepted ledger/storage boundary.
- Status: `accepted`
- Accepted at: `2026-08-13 20:19:21 +0800`
- User decision: `authorized`
- Design authority:
  `docs/plans/data-storage-runtime-projection-phase1-contract-20260813.md`
- Artifact path:
  `docs/gateflow/data-storage-runtime-projection-p3a/goal-confirmation.md`

## Plain-language decision

Phase 3A has two separate costs and therefore needs two coordinated fixes:

1. **Computation cost:** every normal trade write currently loads and replays all
   `trade_events`, so CPU remains O(event history). The accepted 10,000-event
   retained-closed-lot evidence failed the frozen full-writer budget.
2. **Publication cost:** `replace_position_lots()` currently deletes the entire
   table and reinserts every lot, so SQLite DML/WAL is O(all lots), even when one
   lot changed.

Changed-lot diff publication fixes only item 2. Checkpoint plus deterministic
tail replay fixes item 1. Neither is a safe substitute for the other. The
work unit must keep both while preserving `trade_events` as the sole trading
authority.

## Goal

Make the normal, append-safe trade-write projection path depend on a bounded
verified checkpoint, the strict event tail, and physically changed lots—not on
the complete event history and all stored lots—without changing ledger
authority, projection semantics, account isolation, or explicit historical
replay behavior.

## Motivation and direct evidence

- `src/application/ledger/writer.py::persist_trade_event_object()` and
  `rebuild_position_lots_from_trade_events()` call `list_trade_events()` and
  `project_stored_trade_events_to_position_lots()` for a full deterministic
  replay.
- `src/application/ledger/repository.py::replace_position_lots()` executes an
  unconditional `DELETE FROM position_lots`, then inserts every projected lot.
- Phase 1 formal evidence for 5,000 retained closed lots measured about
  2.849 seconds p95 wall and 2.739 seconds p95 CPU, failing the frozen
  2-second / 1.5-second full-writer gate.
- The accepted Phase 1 contract therefore blocks Phase 3A full-replay shipping
  and requires a focused checkpoint/delta planreview before implementation.
- Current synchronous publication consumes projected lots, publishability, and
  bounded diagnostic facts. Complete historical allocations are exposed by an
  explicit query path and remain on the canonical full replay.

## Success signals

1. For an append-safe tail no larger than 1% of at least 10,000 historical
   events, checkpoint/tail publication:
   - is exactly equivalent to full replay for current lots, risk views, public
     `position_lots`, publishability, and canonical fingerprints;
   - matches the full oracle's exact successful diagnostic result; the Phase
     3A v1 fast path is eligible only when that list is empty;
   - improves p95 wall and CPU by at least 50% versus forced-full execution of
     the same append operation on an independently reset identical fixture;
   - has p95 wall and CPU no greater than 500 ms;
   - respects the Phase 1 peak-allocation and checkpoint-size budgets.
2. Diff publication changes only added, changed, and removed `record_id` rows
   plus required head rows. There is no global lot-table delete/reinsert.
3. Void, repair, backdated insert, controlled update/delete, schema change,
   prefix mismatch, and unclassified mutation never use an unsafe checkpoint.
   They select a provably unaffected earlier checkpoint or full replay.
4. `trade_events` remains the only event authority and `position_lots` remains
   the only current-lot projection. Deleting every checkpoint still permits an
   exact full rebuild.
5. At most three verified/recoverable checkpoint rows are retained, and a
   checkpoint is a versioned cache bound to an exact event prefix—not a second
   ledger.
6. Global source generation, per-account lot generation, projection heads,
   fingerprints, and indexed trusted reads fail closed on mismatch and publish
   atomically with the lot diff.
7. Explicit historical allocation/diagnostic/replay APIs continue using full
   replay and do not silently consume bounded checkpoint summaries.

## Scope boundary

Included:

- additive normalized account columns/indexes required by Phase 3A;
- source/lot generations and per-account projection heads;
- canonical lot fingerprinting and trusted account-scoped current-position
  reads;
- `record_id`-based changed-lot diff publication;
- one domain-owned resumable projector accumulator used by both full and tail
  entry modes;
- bounded SQLite checkpoint storage, append-safety classification,
  conservative invalidation, fallback, and explicit verifier status;
- writer-facade migration to one transactional publication boundary;
- deterministic equivalence, mutation-matrix, concurrency/recovery, and
  reference performance tests;
- read-only status/verification surfaces and operator documentation.

Excluded:

- replacing, compacting, deleting, or weakening `trade_events`;
- a general unrestricted per-event delta projector or a second ledger;
- Phase 3B `current_decision_projection` and its lifecycle/combo/assigned-stock
  revisions;
- changing lifecycle evidence retention, research tiering, scan blobs, or
  Strategy Lab dataset policy;
- serving complete prefix allocations or every historical diagnostic from a
  checkpoint;
- repair/replay inside scan, tick, ordinary quality, or trusted reads;
- production migration, runtime-config activation, release, deployment,
  notification, broker write, or data deletion.

## Implementation boundary and sequencing principle

The implementation plan must use an expand-and-verify sequence:

1. add additive schema, generation/head contracts, canonical fingerprints, and
   diff publication while retaining the full replay oracle;
2. add the shared resumable domain projector and bounded checkpoint store;
3. run checkpoint/tail in shadow against full replay across the complete
   invalidation matrix and frozen performance fixtures;
4. enable the internal fast publication entrypoint only if every correctness,
   time, space, and recovery gate passes; otherwise stop with the existing full
   replay path still authoritative.

No implementation slice may remove the full replay oracle or make checkpoint
availability necessary for correctness.

## Why this is not over-designed

- The checkpoint path is now required by a measured hard-gate failure; it is no
  longer speculative optimization.
- The design adds one bounded cache and one existing-projection diff, not a new
  event authority, orchestration platform, or general incremental framework.
- It reuses the domain projector and SQLite transaction boundary rather than
  duplicating business rules in application code.
- It defers Phase 3B and all unrelated lifecycle/research retention work.

## Proposed stop conditions

- Any full-oracle mismatch, ambiguous append-safety classification, unbounded
  checkpoint state, or unsafe mixed-version writer blocks fast-path activation.
- Failure of any frozen time/space budget returns the design to planreview; the
  threshold is not relaxed.
- Discovery that a synchronous writer requires complete historical allocation
  or diagnostic lists blocks the bounded checkpoint payload until that caller
  is explicitly separated or retained on full replay.
- No production migration or activation is authorized by this work unit.

## Accepted boundary

Phase 3A may implement both changed-lot diff
publication and bounded checkpoint/tail computation, but fast-path cutover is
conditional on shadow equivalence plus all frozen time/space gates; Phase 3B,
production migration, release, deployment, and deletion remain separate.

The next Gateflow entry point is `plan`, followed by a
DeepSeek v4 Pro `planreview` focused on time/space efficiency, invalidation,
mixed-version safety, and whether the plan is the smallest credible solution.
