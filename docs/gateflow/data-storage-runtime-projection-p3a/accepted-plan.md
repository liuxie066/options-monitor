# Gateflow Accepted Plan — data-storage-runtime-projection-p3a

- Work unit: `data-storage-runtime-projection-p3a`
- Gate: `goal -> plan -> plan review -> fix -> re-review`
- Decision: `pass-with-risks`
- Base: `origin/main@2a7bc5668ecda53d0b0d1c3d8856d976487b5d6f`
- Goal:
  `docs/gateflow/data-storage-runtime-projection-p3a/goal-confirmation.md`
- Plan: `docs/gateflow/data-storage-runtime-projection-p3a/plan.md`
- First review: `docs/reviews/plan-review-20260813-210003.md` (`fail`)
- Fix: `docs/gateflow/data-storage-runtime-projection-p3a/plan-review-fix.md`
- Accepted re-review:
  `docs/reviews/plan-review-20260813-221516.md` (`pass-with-risks`)
- Artifact path:
  `docs/gateflow/data-storage-runtime-projection-p3a/accepted-plan.md`

## Accepted decision

Phase 3A replaces the normal append-safe write cost of complete event replay
plus global lot delete/reinsert with a bounded active-only resumable state,
strict ordered tail, exact changed-lot diff, and bounded account-head
publication. `trade_events` remains authority; `position_lots` remains the
physical current-lot projection; checkpoints remain disposable caches.

## Approved implementation order

1. S1: additive account/generation/head schema, exact lot diff publication,
   canonical fingerprints, loaded implementation fingerprint, and trusted read
   under the unchanged full oracle. Checkpoint mode stays disabled.
2. S2: one shared full/resumable domain transition and exact stateful public-row
   folding. Full replay remains the oracle; no checkpoint persistence/cutover.
3. S3: bounded checkpoint store, strict-tail runtime, invalidation, rotation,
   pruning, and internal forced-full/fast-if-safe entrypoints; public writers
   remain full-only.
4. S4: preflight/writer facade integration, exact idempotency, inventory, and
   read-only full-versus-tail shadow evidence.
5. S5: explicit migration/readiness/verify/activate/deactivate facades,
   deterministic benchmark/acceptance evidence, and operator docs. No live
   apply or activation is performed by implementation acceptance.

Every slice requires focused validation, adversarial DeepReview, finding fix,
re-review, and a slice commit before the next slice starts.

## Frozen efficiency boundaries

- Reference append: p95 wall and CPU each <=500 ms with tail <=1% of at least
  10,000 events, and each at least 50% faster than the same public facade and
  candidate in forced-full mode on an independently reset identical database.
- Fast paths read/hash no event prefix, decode no full lot list, and write only
  event/touched lots/bounded heads plus a checkpoint only at rotation.
- Checkpoint count K<=3; active-only state does not retain closed lots or a
  `close_event_ids` vector; final close emits the public row then evicts state.
- No persisted cadence counters: row/byte cadence comes from the bounded strict
  tail since the last checkpoint boundary.
- Candidate idempotency is bounded by submitted ids; eligible close resolution
  uses active state plus at most one explicit lot lookup.
- Populated backfill/index building is explicit migration work, never startup.
- Loaded semantic source bytes are hashed once per process; transaction checks
  are cached O(1). AST import closure fails on unclassified first-party edges.
- The frozen flat account fingerprint remains streamed O(L_a). The accepted
  fixture is a hard activation gate; `retained_lots_10x` is mandatory capacity
  evidence with `retained_lots_10x_guarantee=false`, not an undeclared promise.

## Finding status

- P3A-PR-01 through P3A-PR-08: `已修复`.
- Final implementation-fingerprint closure, no-close-vector, and mixed-batch
  findings: `已修复`.
- Flat retained-row capacity and semantic/nonsemantic classification:
  controlled residuals with explicit evidence/review destinations.
- DeepSeek v4 Pro final conclusion: `pass-with-risks`, no blocker.

## Authority boundary

This acceptance authorizes source implementation and temp/test database
validation only. It does not authorize live ledger migration or activation,
production config/service changes, merge, release, deployment, notification,
broker writes, or data deletion.

## Next gate

Accepted-plan commit, then S1 implementation only.
