# Gateflow PlanReview Fix — data-storage-runtime-projection-p3a

- Gate: `plan review fix`
- Work unit: `data-storage-runtime-projection-p3a`
- Plan: `docs/gateflow/data-storage-runtime-projection-p3a/plan.md`
- First review: `docs/reviews/plan-review-20260813-210003.md`
- Reviewed base: `origin/main@2a7bc5668ecda53d0b0d1c3d8856d976487b5d6f`
- Final re-review:
  `docs/reviews/plan-review-20260813-221516.md` (`pass-with-risks`)
- Status: `accepted; all blocking findings fixed`
- Artifact path:
  `docs/gateflow/data-storage-runtime-projection-p3a/plan-review-fix.md`

## Scope refresh

The isolated plan branch was fast-forwarded from the Phase 1 merge commit to
current `origin/main@2a7bc566`. The intervening two commits change release and
dependency-graph metadata only; they do not alter ledger or storage source.
The original dirty worktree remains untouched.

## Finding dispositions

### P3A-PR-01 — accepted — fixed in plan

The resumable state is now explicitly active-lot-only. A final close emits the
exact final public row and then evicts domain and publication continuation
state; closed history remains in authoritative `position_lots`, not in every
checkpoint. The plan requires seeded prefix-fork property tests and a
retained-closed history-growth fixture whose checkpoint bytes stay invariant.
Targets to evicted rows and lot-id collisions select full mode through bounded
record-id lookup.

### P3A-PR-02 — accepted — fixed in plan

V1 checkpoints are admitted only from an exactly empty effective diagnostic
list. Any tail diagnostic, including a future warning, selects the full path.
Only a fixed empty sentinel/count is stored; complete diagnostic APIs remain on
full replay. This removes an unnecessary O(E) diagnostic chain and preserves
exact successful writer behavior.

### P3A-PR-03 — accepted — fixed in plan

The plan now requires primary-key batch lookup for candidate event ids, active
state for eligible FIFO/untargeted close resolution, and fail-on-call spies for
`list_trade_events()`, `list_position_lots()`, full projector entrypoints, and
global replacement on every fast facade. It also separates explicit rebuild
timing from operation-equivalent append timing: forced-full and fast executions
use the same public facade/candidate and independently reset identical DBs.

### P3A-PR-04 — accepted — fixed in plan

Normal semantic invalidation now invalidates K<=3 checkpoint rows but leaves an
enabled global mode enabled. The full fallback publishes exact lots/heads and
seeds a new full checkpoint atomically. Only corruption, schema-cookie mismatch,
or semantic shadow mismatch sets global `untrusted` and requires explicit
verify/reactivate.

### P3A-PR-05 — accepted in part — fixed in plan

The DeepSeek claim that global source generation necessarily makes unrelated
accounts unavailable was rejected: the accepted design writes the same final
global generation into all existing heads in every successful event
transaction. The missing proof was accepted and added as A-write/B-read tests
and account-fanout measurement. The plan also adds a closed per-column trigger
classification, INSERT/UPDATE/DELETE/REPLACE matrix, and activated SQLite schema
cookie so a new unclassified column cannot remain falsely fresh. Event/lot
primary keys and normalized account columns are explicitly classified as
projection-affecting; metadata timestamps alone are excluded.

### P3A-PR-06 — accepted — fixed in plan

Forced rotation at both the 100-event and 1-MiB boundaries now has its own
5-warmup/30-repetition distribution and independent p95 wall/CPU <=500 ms
gate, plus the existing allocation/WAL/K limits. Ordinary transactions cannot
hide a rotation spike.

### P3A-PR-07 — accepted — fixed in plan

Populated-table index construction and the existing repeated
`_backfill_position_lot_contract_columns()` scan have moved out of startup and
into explicit migration apply. Startup creates indexes only for empty/new
tables and otherwise reports not-ready/full-only without an O(history) scan.
Migration reports account/scalar backfill and index-build wall/CPU/WAL.

### P3A-PR-08 — accepted — fixed in plan

The prefix chain now has exact seed, canonical persisted event bytes,
eight-byte big-endian length framing, and raw-digest recurrence. Backdated
invalidation compares only against at most three checkpoint boundaries;
control/update/delete invalidates all K directly. No event/lot history scan is
used to decide invalidation.

## Additional efficiency decisions

- Exact current public fields are folded once per active lot rather than
  retaining every strategy-adjust identity or rebuilding fields from prefix
  events.
- Head `lot_count` preserves existing writer return values through O(A) account
  totals without table-wide count/list work.
- Changed-account fingerprinting streams ordered stored rows with bounded
  allocation. It remains O(retained rows for changed accounts) because the
  accepted fingerprint is a flat canonical SHA-256. A Merkle side projection is
  deliberately not added unless this measured residual fails the 500-ms gate.
- Force-full Combo/control/rebuild paths are measured and reported separately.
  The Phase 1 failing fixture is open/close history, not proof that Combo
  membership is the dominant workload, so no speculative Combo index/cache was
  added.
- Activation binds semantic projector schema, the exact loaded implementation,
  acceptance source revision, SQLite schema cookie, current source generation,
  and fingerprints. Source bytes are streamed once at process initialization;
  transactions compare a cached digest and perform no file reads/hashes.

## Second-adviser dispositions

### P3A-PR2-N1 — rejected as stated; boundary clarified and tested

DeepSeek treated every Combo Yield/SP+LC append as the special Combo identity
writer. Source contradicts that assumption. Ordinary broker/manual events can
carry `strategy_snapshot`, `strategy_group_id`, `same_expiry_pair`, or
`combo_yield` metadata and still use the ordinary single/batch event facades.
Only the second-leg plus immutable identity transaction and later
adoption/reconciliation inspect or mutate broader membership. The plan now
requires ordinary Combo-metadata single/batch fast-path parity and benchmark
coverage, while measuring those special relationship transactions as
force-full. Strategy labels alone may not select slow mode.

### P3A-PR2-N2 — accepted — fixed in plan

`projector_schema` plus source commit was not sufficient to detect an installed
projector semantic edit whose author forgot a version bump. The plan now adds a
versioned closed semantic-source manifest and exact loaded implementation
fingerprint. It covers domain transition/import/public-record/fingerprint
dependencies, is recomputed by CI, is stored in source state, heads,
checkpoints, and acceptance evidence, and is hashed only once per process.
Missing/different source becomes untrusted before tail use. This closes drift
without per-transaction file I/O.

### P3A-PR2-N3 — controlled by accepted upstream contract

Global generation and all-head publication are binding Phase 1 decisions
because void/repair and cross-event validation have not been proven
account-partitionable. Replacing them with lazy/per-account generation here
would change the accepted architecture boundary. The existing
`account_fanout=max(10, 5x baseline)` hard gate remains: failure returns the
generation design to planreview rather than relaxing the limit.

### P3A-PR2-N4 — accepted as measured residual; failure semantics fixed

The exact frozen flat fingerprint remains O(retained rows for the changed
account). The plan now reports its row/byte/time cost separately, runs an
additional `retained_lots_10x` capacity diagnostic, and states the consequence:
missing the accepted 500-ms gate means `not_ready` and activation rejection.
There is no transaction-time switch to forced-full because that path needs the
same exact fingerprint after adding O(E) replay. Post-activation degradation is
surfaced in status and supports explicit deactivation; a Merkle/fingerprint
contract change still requires a later measured review.

### P3A-PR2-N5/N6 — accepted — documented and tested

Zero diagnostics is now an explicit v1 liveness boundary with fallback reason
telemetry. Trusted current reads are explicitly sourced from all authoritative
account `position_lots`, including retained closed rows, and never from the
active-only accumulator; final-close read/fingerprint parity is required.

## Final incremental adviser dispositions

DeepSeek v4 Pro re-read the current 20-section plan under marker
`FINAL-INCREMENT-2204` and returned `pass-with-risks`, with no blocker.

### P3A-PRR-M1 — accepted — fixed in plan

Semantic manifest closure is now machine-enforced. Python AST resolves absolute
and relative first-party imports to normalized paths, walks to a fixed point,
and requires exact discovered nodes/edges to match the closed classification.
Unlisted dynamic imports, unresolved edges, and added/removed reachable files
fail CI. Path/file bytes have exact length framing, and runtime hashing happens
once per process.

### P3A-PRR-M2 — accepted as controlled residual

The plan does not invent a new 10x guarantee beyond the accepted goal.
`retained_lots_10x` is mandatory diagnostic evidence whose manifest records
`retained_lots_10x_guarantee=false`, exact rows/bytes/wall/CPU, and a capacity
warning. The accepted fixture remains a hard <=500-ms activation gate; failure
is `not_ready`. Promoting 10x into a guarantee requires later measured
planreview.

### P3A-PRR-M3 — accepted — fixed in plan

The resumable serialized lot schema explicitly contains no
`close_event_ids` vector. It keeps scalar last-event facts and aggregate
economics; the physical public row keeps its existing scalar
`last_close_event_id`, while full historical `ProjectionResult` retains the
complete close list. Field-level and repeated-partial-close space assertions
freeze this boundary.

### P3A-PRR-M4 — accepted — fixed in plan

Mixed existing/new atomic batches now have an exact contract and regression:
existing ids reuse stored immutable cash conversions before canonical-byte
comparison and cause no replay/generation/head/rotation write; new ids alone
receive conversions and enter the strict ordered tail.

## Final sequencing correction

The final plan follows the accepted Phase 1 expand-and-verify order:

1. S1 lands additive schema/generations/diff/trusted read and the loaded-source
   fingerprint under the unchanged full oracle, with checkpoint mode disabled.
2. S2 factors one shared resumable/full transition and updates the semantic
   digest in the same slice, without checkpoint persistence or cutover.
3. S3 adds bounded checkpoint/runtime internally; S4 integrates facades and
   shadow comparison; S5 creates acceptance/activation tooling and evidence.

Persisted checkpoint cadence counters were removed as redundant. The already
required strict-tail stream is the complete interval from the last checkpoint
boundary, so row/byte rotation accounting is bounded and needs no additional
mutable checkpoint state.

## Final re-review result

Final review artifact:
`docs/reviews/plan-review-20260813-221516.md`.

Conclusion: `pass-with-risks`. P3A-PR-01 through P3A-PR-08 and final M1/M3/M4
are `已修复`; retained fingerprint scaling and reviewed semantic classification
are controlled residuals with named gates/destinations. No implementation had
begun at acceptance time.
