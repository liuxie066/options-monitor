# Gateflow Plan — data-storage-runtime-projection-p3a

- Gate: `plan`
- Work unit: `data-storage-runtime-projection-p3a`
- Base: `origin/main@2a7bc5668ecda53d0b0d1c3d8856d976487b5d6f`
- Status: `accepted; final re-review pass-with-risks`
- Goal confirmation:
  `docs/gateflow/data-storage-runtime-projection-p3a/goal-confirmation.md`
- Design authority:
  `docs/plans/data-storage-runtime-projection-phase1-contract-20260813.md`
- First review:
  `docs/reviews/plan-review-20260813-210003.md` (`fail`)
- Review-fix artifact:
  `docs/gateflow/data-storage-runtime-projection-p3a/plan-review-fix.md`
- Final re-review:
  `docs/reviews/plan-review-20260813-221516.md` (`pass-with-risks`)
- Artifact path:
  `docs/gateflow/data-storage-runtime-projection-p3a/plan.md`

## 1. Goal, motivation, and success signal

### Goal

Replace the normal append-safe position-publication cost with:

```text
bounded resumable current state
+ strict ordered event tail
+ physically changed lot rows
+ bounded account-head publication
```

instead of:

```text
all trade_events
+ a complete delete/reinsert of position_lots
```

`trade_events` remains the sole trading authority. `position_lots` remains the
sole physical current-lot projection. Runtime checkpoints are disposable,
versioned caches; deleting all of them must still permit an exact full rebuild.

### Motivation

The merged Phase 1 evidence proved two independent costs:

- `project_stored_trade_events_to_position_lots()` is O(event history), and the
  retained-closed-lot writer measured about 2.849 seconds p95 wall / 2.739
  seconds p95 CPU on the reference host, failing the frozen 2-second /
  1.5-second gate;
- `replace_position_lots()` executes a global `DELETE`, then reinserts every
  projected lot, causing O(all lots) DML/WAL even when one lot changed.

The source also shows a third end-to-end cost: ordinary open/close/adjust
preflight currently performs its own full replay before the writer performs a
second full replay. Optimizing only the final publication would leave user
latency O(event history).

### Completion signals

The work unit is complete only when all of the following are true:

1. Full replay and resumed tail projection share one domain state transition
   implementation; no application-layer copy of position business semantics
   exists.
2. Full replay remains the oracle for rebuild, audit, complete economic
   allocations, and complete historical diagnostics.
3. Append-safe current publication is equivalent to the full oracle for:
   active lot economics, risk views, every touched/finalized public
   `position_lots` row, the complete resulting stored projection,
   publishability, per-account fingerprints, and the exactly empty successful
   diagnostic list. A checkpoint carrying any diagnostic is ineligible in v1.
4. The accepted reference fixture with tail no larger than 1% of at least
   10,000 events has p95 wall and CPU no greater than 500 ms and improves each
   by at least 50% versus forced-full execution of the same public append
   facade/candidate on an independently reset identical database. The failing
   Phase 1 full-writer rebuild remains the trigger evidence.
5. Fast publication performs zero full-prefix event reads/hashes and zero
   decoded full-lot-list reads, changes only added/changed/removed lot rows plus
   bounded head/checkpoint rows, and never executes global lot-table
   delete/reinsert. Exact account fingerprinting may stream that changed
   account's stored rows with bounded allocation; it is measured explicitly.
6. Checkpoint decode/serialization peak allocation, retained checkpoint bytes,
   rotation WAL, checkpoint count, and account fanout stay within the resource
   contracts below.
7. Every ambiguous mutation uses full replay or fails closed. No unsafe tail
   result is published.
8. A current-position read uses one SQLite read transaction, indexed account
   lots, exact live-generation equality, and the shared fingerprint; it performs
   zero event reads, replay calls, or writes. It returns every retained account
   row, including closed rows; it never substitutes the active-only checkpoint
   accumulator for the authoritative stored projection.
9. All event-writing facades either use the common transactional publisher or
   are explicitly classified as full-oracle operations; no direct projection
   writer bypass remains. Candidate-event idempotency uses primary-key lookups,
   and eligible close resolution uses the active resumable state rather than
   listing retained closed lots.
10. Additive migration is explicit inventory -> apply -> verify. Repository
    startup never performs an O(history) account/scalar backfill, populated
    index build, checkpoint build, or repeated compatibility scan.
11. Source implementation, tests, and operator docs are complete, but no live
    ledger migration/activation, release, deployment, notification, broker
    write, or historical deletion is performed.
12. Fast-path eligibility is bound to the exact loaded projector implementation
    fingerprint accepted for that store. The fingerprint is computed once per
    process with bounded streaming I/O; ordinary transactions perform only O(1)
    cached comparisons and never hash source files.

## 2. Non-goals and scope boundary

### Included

- additive normalized `account` columns and indexes for `trade_events` and
  `position_lots`;
- global event-source generation, per-account lot generations, projection
  heads, exact account fingerprints, and trusted current-position reads;
- `record_id` lot diff publication;
- one resumable domain accumulator containing active lots only plus
  application-owned exact current public fields for those active lots;
- bounded SQLite checkpoints, strict-tail reads, conservative invalidation,
  payload integrity, rotation, pruning, and full-oracle verification;
- ordinary append-safe preflight and writer integration;
- explicit migration/readiness/shadow/activation artifacts and CLI surfaces;
- deterministic correctness, crash/recovery, mixed-version, time, space, WAL,
  allocation, and query-plan tests;
- operator documentation.

### Excluded

- replacing, compacting, deleting, or weakening `trade_events`;
- a general event-sourced framework, unrestricted delta projector, second
  ledger, distributed lock, service, queue, or new scheduler;
- Phase 3B `current_decision_projection`, lifecycle/combo/assigned-stock
  revision triggers, and ordinary quality cutover;
- lifecycle evidence or scan/research retention changes;
- serving closed-lot history or complete historical allocation/diagnostic lists
  from a checkpoint;
- making a checkpoint required for correctness;
- changing public trading rules, candidate selection, cash calculations,
  notifications, broker APIs, or runtime configuration;
- running any production migration, activation, release, deployment, or data
  deletion in this work unit.

## 3. First-principles decision and direct code evidence

### Decision

Changed-lot diff and checkpoint/tail solve different terms and are both
required:

| Cost | Current owner | Required fix |
|---|---|---|
| event computation | `domain/domain/ledger/projection.py` called through `publisher.py` | resumable shared accumulator + strict tail |
| public-record conversion | `src/application/ledger/publisher.py` scans complete event context | resumable publication context using the same full/tail state path |
| lot DML/WAL | `repository.py::replace_position_lots()` | canonical `record_id` diff |
| preflight latency | `preflight.py` runs before/after full projections | trusted current accumulator + candidate tail preview |
| freshness | current lots have no source/head binding | generations, heads, canonical fingerprints |

### Evidence

- `writer.py::persist_trade_event_object()` lists every event, projects all of
  them, and then calls `replace_position_lots()`.
- `preflight.py::_current_trade_event_projection()` and
  `_preflight_trade_event_append()` list/replay complete history before the
  writer does so again.
- `repository.py::replace_position_lots()` executes unconditional
  `DELETE FROM position_lots`.
- `publisher.py::_position_lot_to_legacy_record()` needs open-event fields,
  applied strategy patches, and the last close event. A current checkpoint must
  retain the exact folded current public fields plus the active lot's canonical
  open event/economic state, but it does not need every prior patch, close,
  allocation, or diagnostic row.
- `PositionLot.close_event_ids` is used by the current full oracle only to
  locate the last close event. The physical public row exposes the scalar
  `last_close_event_id`; it does not expose the complete close-id vector.
  Copying that tuple or every closed lot into every checkpoint would make
  checkpoint size grow with lifetime history. The resumable schema therefore
  contains no event-id array: it stores active lots only, aggregate/current
  economics, exact current public fields, and scalar last-event facts. A fully
  closed row is emitted to `position_lots` then evicted from the accumulator.
  Full replay retains the complete list for explicit historical APIs.
- `persist_trade_event_object()` and its atomic batch variant currently call
  `list_trade_events()` just to build an idempotency/cash-conversion map before
  the projection replay. That hidden O(E) read must become a candidate-id
  primary-key lookup.
- `lot_resolver.py` currently lists all `position_lots`, including retained
  closed rows, before filtering. Eligible fast close resolution must use the
  active accumulator; an explicit target absent there uses one record-id lookup
  and selects full mode when it refers to a retained closed row.
- Repository initialization currently runs
  `_backfill_position_lot_contract_columns()` over all stored lots. The new
  migration must retire that repeated startup scan, not merely avoid adding a
  second account backfill.
- `queries.py::trade_event_economic_allocations()` is an explicit full-replay
  allocation API and must remain one.
- `trades/resolver.py` and publisher tests prove that an ordinary broker/manual
  event can carry `combo_yield`, `strategy_snapshot`, `strategy_group_id`, or
  `same_expiry_pair` metadata and still use the ordinary single/batch event
  facade. A strategy label never selects full mode by itself.
- `writer.py::persist_trade_event_with_combo_identity()` is different: it
  atomically publishes a second leg, immutable group identity, and resolved
  membership after inspecting broader event/lot relationships. That special
  write topology, adoption/reconciliation, and control events are correctness
  fallbacks in v1; they are not silently declared tail-safe.

## 4. Complexity and resource contract

Let:

- `E` = total canonical trade events;
- `S` = serialized resumable current-position state;
- `T` = strict ordered suffix after the selected checkpoint;
- `C` = changed/added/removed lot records;
- `L_a` = current stored lots for one account;
- `A` = accounts with projection heads;
- `K` = retained runtime checkpoints.

| Operation | Required time | Required space/write behavior |
|---|---|---|
| explicit full rebuild/audit | O(E + all projected lots) | full oracle allowed; diff DML still replaces no unchanged rows |
| fast preflight/publication | O(decode S + T + touched active state + streamed fingerprint rows + A) | zero full-prefix reads/hashes and zero full decoded lot-list reads; DML O(C + A) |
| trusted current read | O(L_a) | one account-indexed result + bounded hash allocation |
| ordinary append between rotations | same as fast publication | zero checkpoint-row writes; one event plus C lots and A heads |
| checkpoint rotation | same projection cost + O(S) serialization | exactly one new checkpoint and bounded pruning; K <= 3 |
| invalidation lookup | indexed/bounded metadata work | no accumulator decode required before selecting fallback |

Frozen acceptance limits:

- checkpoint/tail p95 wall <= 500 ms and p95 CPU <= 500 ms when `T <= 1%`
  of at least 10,000 events;
- the same public append facade and candidate are run against independently
  reset identical databases in forced-full and fast modes; fast wall and CPU
  each improve by at least 50% versus that forced-full result. The Phase 1
  rebuild timing remains the evidence that triggered this design, not a
  substituted denominator for a different operation;
- peak Python allocation <= `max(64 MiB, 2 * checkpoint_payload_bytes)`;
- K <= 3;
- steady-state checkpoint bytes <= `3 * one_resumable_state_bytes + 10%`
  metadata;
- no-prefix-mutation fixture has zero full-prefix reader/hash calls;
- candidate idempotency reads at most the submitted event ids by primary key;
- eligible close resolution reads only the active accumulator plus at most the
  explicitly targeted record row; it never lists retained closed lots;
- invalidation lookup p95 <= 50 ms;
- current trusted read p95 <= 50 ms/current scale and <= 200 ms at
  `current_state_10x`, with peak allocation <= 16 MiB/current scale;
- account-fanout publication retains the Phase 1 <=250 ms wall, <=200 ms CPU,
  <=64 MiB allocation budget;
- a checkpoint rotation reports peak `db + wal + shm`; growth attributable to
  the rotation must be <= `max(64 MiB, 2 * checkpoint_payload_bytes)`, and after
  `wal_checkpoint(TRUNCATE)` the database must return to the K<=3 steady-state
  envelope;
- between rotations, checkpoint table row writes and checkpoint payload bytes
  written are exactly zero.
- changed-account fingerprinting is a stable ordered SQLite cursor -> canonical
  hash stream; it never materializes every retained row as a Python object and
  remains inside the same 500-ms/peak-allocation gate.
- the accepted retained-closed reference fixture reports fingerprint-only rows,
  bytes, wall, and CPU separately from checkpoint decode/tail work. A mandatory
  `retained_lots_10x` diagnostic run estimates the later capacity boundary but
  has no pass/fail guarantee in this work unit and does not silently enlarge
  the accepted 10,000-event success criterion. The acceptance manifest records
  `retained_lots_10x_guarantee=false` plus its rows/bytes/wall/CPU and an
  explicit capacity warning when it exceeds current hard limits; only a later
  measured planreview may promote that diagnostic into a new activation gate.
- if the changed-account flat fingerprint or combined append misses the frozen
  gate on the accepted fixture, readiness is `not_ready` and activation is
  rejected. There is no per-transaction stopwatch fallback: forced-full also
  needs the same exact fingerprint and adds O(E) replay, so switching after a
  slow scan would be both later and slower. Post-activation degradation is
  surfaced by per-write telemetry/status and the operator can explicitly
  deactivate checkpoint mode while correctness remains unchanged.
- loaded-projector fingerprint construction is measured once per process,
  streams bounded source files, reads no ledger history, and has p95 wall/CPU
  <= 50 ms and peak allocation <= 8 MiB on the reference host. Fast and trusted
  transactions perform zero source-file reads and only cached digest equality.

The v1 rotation cadence is a code/contract constant, not runtime tuning:

```text
rotate after 100 tail events OR 1 MiB canonical tail bytes, whichever comes first
```

This keeps the 10,000-event reference tail at or below 1%, bounds an unusual
large-event suffix, and prevents writing O(S) checkpoint bytes on every trade.
Both the no-rotation and rotation-boundary transactions are measured as
separate 5-warmup/30-repetition distributions. Forced rotation at the
100-event boundary and at the 1-MiB boundary must each satisfy p95 wall <= 500
ms and p95 CPU <= 500 ms, in addition to the WAL/allocation/K limits above.
Cycle p99 and maximum are reported as diagnostics and cannot replace p95.

## 5. Ownership and dependency direction

### Domain owner

`domain/domain/ledger/` owns:

- event validation and deterministic transition semantics;
- `ResumableProjectionState` and its versioned canonical active-lot payload;
- current lot/risk state required for future tail application;
- open-event fee inputs, allocated-open-fee state, applied adjust state,
  last-close state, and publishability inputs for active lots only;
- full-oracle and tail entry modes backed by the same transition functions;
- canonical `position_lots_fingerprint.v1`.

The domain layer imports neither SQLite nor `src/`.

### Application owner

`src/application/ledger/position_projection_publication.py` first owns the
full-oracle lot diff and generation/head publication boundary. S1 routes every
writer through that narrow boundary while checkpoint mode is disabled.
`src/application/ledger/position_projection_runtime.py` later composes the same
publication boundary and owns the canonical checkpoint envelope, which contains
the domain state above plus the application-owned
`ResumablePublicationState`. The application owns:

- mutation classification and fast/full selection;
- checkpoint selection, decode/hash verification, tail query orchestration,
  cadence, and pruning decisions;
- application publication context needed to emit exact legacy
  `PositionLotRecord` fields for active, touched, and newly finalized lots
  without scanning prefix events;
- lot diff construction, account/head publication order, and result metadata in
  the shared publication boundary rather than duplicated fast/full paths;
- preflight snapshot tokens and same-transaction writer revalidation;
- fallback and failure classification.

`publisher.py` remains the single public-record conversion owner. Full and
tail modes call the same stateful conversion functions.

S1 fingerprints the unchanged full-oracle roots and publishes only full-oracle
heads. S2 changes semantic roots and therefore must update the checked-in
manifest/expected digest in the same slice; no S1 head becomes trusted under
the new binary until a full-oracle publication records that new fingerprint.
S3 may then bind checkpoints to it. This prevents a mixed-slice placeholder or
an old trusted head from crossing a semantic implementation change.

`src/application/ledger/projector_implementation.py` is a narrow application
guard, not a second projector. It owns a versioned, checked-in manifest of the
closed semantic source set used by the domain transition, event import, public
record folding, and canonical fingerprint. At process startup it streams those
exact runtime `.py` bytes in sorted normalized relative-path order through
length-framed SHA-256 and caches the result. The digest frames the canonical
manifest bytes, then for each semantic file its UTF-8 relative-path byte length,
path bytes, raw-file byte length, and raw file bytes; all lengths are unsigned
eight-byte big-endian values. A generated expected digest is committed with the
manifest; a CI test recomputes it, so changing any declared semantic file or
classification without updating the implementation fingerprint fails. A
runtime source file that is missing, unreadable, outside the repository/release
root, contains a non-canonical path, or differs from the expected digest makes
the checkpoint path unavailable. Raw-byte identity is deliberately
conservative: a checkout that rewrites line endings fails closed rather than
silently reusing a checkpoint. The startup resolver supports both a Git
checkout root and the release install root used by `scripts/install.sh`; it
never requires `.git` at runtime or guesses from `VERSION` alone.

### SQLite owner

`repository.py` owns only persistence primitives:

- additive tables/columns/indexes/triggers;
- ordered prefix/tail queries, candidate-event primary-key lookup, explicit
  lot-id lookup, and active-lot indexed queries;
- checkpoint/head/source-state reads and writes;
- canonical lot-row diff application;
- one read transaction for trusted account lots;
- migration inventory/apply primitives.

Triggers invalidate and increment counters only. They never replay events,
decode an accumulator, build records, or publish a trusted head.

### Public boundary

`src/application/ledger/api.py` exports application facades. Non-ledger modules
must not import repository/checkpoint internals.

## 6. Exact data contracts

Names may be changed mechanically during implementation only if cardinality,
keys, ownership, and transitions remain identical.

### 6.1 Additive normalized columns

```text
trade_events.account TEXT NULL
position_lots.account TEXT NULL
```

Indexes:

```text
trade_events(account, trade_time_ms, event_id)
position_lots(account, expiration, record_id)
```

New writers persist lowercase account values derived from the canonical event
contract key / lot fields. During the mixed-version window, a null normalized
column may be interpreted from canonical JSON only for validation and trigger
scope. A non-null conflict or non-lowercase value aborts. Missing/conflicting
historical values make migration readiness fail; they are never guessed.
On populated stores, both indexes are built only by explicit migration apply;
repository startup does not create them.

### 6.2 `position_projection_source_state`

Singleton row (`singleton_id = 1`):

- `source_generation INTEGER NOT NULL DEFAULT 0`;
- `projector_schema TEXT NOT NULL`;
- `projector_implementation_fingerprint TEXT NULL` during disabled/untrusted
  migration, required non-null before any head/checkpoint can be trusted;
- `sqlite_schema_cookie INTEGER NULL`, captured from `PRAGMA schema_version`;
- `checkpoint_mode TEXT NOT NULL`, one of `disabled`, `enabled`, `untrusted`;
- `last_full_verified_source_generation INTEGER NULL`;
- `updated_at_ms INTEGER NOT NULL`.

`disabled` means full replay + diff only. `enabled` permits a trusted
checkpoint after all guards. `untrusted` means checkpoint corruption or a
semantic shadow mismatch and forbids checkpoint publication until explicit
full verify and later reactivation. Ordinary semantic invalidation changes
checkpoint-row trust, not this global mode. There is no stored `dirty` or
global `invalidated` boolean.

### 6.3 `position_projection_heads`

One row per lowercase account:

- `account TEXT PRIMARY KEY`;
- `lots_generation INTEGER NOT NULL DEFAULT 0`;
- `built_source_generation INTEGER NULL`;
- `built_lots_generation INTEGER NULL`;
- `projection_fingerprint TEXT NULL`;
- `lot_count INTEGER NOT NULL DEFAULT 0`;
- `projector_schema TEXT NOT NULL`;
- `projector_implementation_fingerprint TEXT NULL` during disabled/untrusted
  migration, required non-null when `status=trusted`;
- `status TEXT NOT NULL`, one of `uninitialized`, `trusted`, `untrusted`;
- `updated_at_ms INTEGER NOT NULL`.

Freshness is derived from exact generation equality. A direct event/lot change
does not need to write `status=dirty`; the source/head mismatch is the durable
dirty fact.

### 6.4 `position_projection_checkpoints`

- `checkpoint_id TEXT PRIMARY KEY`;
- `projector_schema TEXT NOT NULL`;
- `projector_implementation_fingerprint TEXT NOT NULL`;
- `prefix_event_count INTEGER NOT NULL`;
- `prefix_end_trade_time_ms INTEGER NOT NULL`;
- `prefix_end_event_id TEXT NOT NULL`;
- `prefix_chain_sha256 TEXT NOT NULL`;
- `source_generation INTEGER NOT NULL`;
- `sqlite_schema_cookie INTEGER NOT NULL`;
- `accumulator_json BLOB NOT NULL` containing canonical UTF-8 JSON;
- `accumulator_sha256 TEXT NOT NULL` over those exact bytes;
- `diagnostic_count INTEGER NOT NULL`;
- `diagnostic_sha256 TEXT NOT NULL`, fixed to the v1 empty sentinel;
- `state_bytes INTEGER NOT NULL`;
- `trust_status TEXT NOT NULL`, `trusted` or `invalid`;
- `verification_kind TEXT NOT NULL`, `full_oracle` or `derived`;
- `parent_checkpoint_id TEXT NULL` as diagnostic metadata, not a foreign key;
- `created_at_ms`, `verified_at_ms`, `invalidated_at_ms`, and
  `invalidation_reason`.

Checkpoint identity is a canonical hash over projector schema, loaded
implementation fingerprint, SQLite schema cookie, prefix binding, source
generation, and accumulator hash. Payload decode first checks byte
length/hash/schema and rejects duplicate/noncanonical keys, NaN/Infinity,
unknown versions, and impossible sizes/counts. The checkpoint schema also owns
the frozen rotation thresholds; a row created under different cadence constants
is schema-incompatible rather than silently reused.

No cadence counter is persisted. Every checkpoint boundary is itself the last
full seed/rotation boundary, so the runtime counts rows and sums canonical event
byte lengths from the same bounded strict-tail stream it already applies. It
rotates when that tail reaches either threshold; a non-rotation transaction
writes no checkpoint row or counter, and a rotation advances the checkpoint
boundary to the tail end. No prefix row is queried or summed for cadence.

The semantic manifest enumerates files explicitly rather than recursively
hashing the whole repository. Its roots are
`domain/domain/ledger/projection.py`, `src/application/ledger/event_codec.py`,
`src/application/ledger/publisher.py`, and the canonical lot-fingerprint helper;
its declared dependencies include ledger events/lots/economics/invariants/
identity/position fields, public position records, and normalization helpers
actually called while producing projection bytes. The manifest also has a
closed classification for every first-party import reachable from those roots:
`semantic-hashed` or `nonsemantic-excluded-with-reason`. Its guard resolves
absolute and relative imports with Python AST to normalized repository-relative
`.py` or package `__init__.py` paths, walks the reachable graph to a fixed point,
and requires the discovered set and edges to equal the manifest classification.
Unlisted dynamic-import primitives in a semantic file fail the guard; any
intentional dynamic first-party target must be enumerated explicitly. Added,
removed, unresolved, or newly reachable first-party edges therefore fail
machine validation rather than relying on reviewer memory. Test,
repository/transaction orchestration, CLI, docs, and benchmark code are not in
the digest only when explicitly excluded with a reviewed reason. This
conservative byte identity does not claim that every byte change is semantic;
it deliberately forces re-acceptance rather than risk an untracked semantic
change.

Pruning keeps the newest two trusted checkpoints plus the newest full-oracle
checkpoint when distinct. The union is at most three. Parent rows may be
pruned, so `parent_checkpoint_id` deliberately has no referential dependency.

### 6.5 Resumable accumulator

The checkpoint envelope has two versioned sections: a domain-owned active-lot
economic state that never imports `src`, and an application-owned publication
state keyed by the same active lot ids. Together they store only facts needed
to continue exact current publication:

- ordered **active** lot state keyed by `lot_id`; no fully closed lot remains;
- the active lot's canonical open-event facts required for future close fee
  allocation;
- allocated open-fee scalar per active lot;
- the exact complete current `PositionLotRecord.fields` mapping per active lot,
  folded through the same publisher transition as full mode;
- last-event/publication facts needed by the next event, never the complete
  close/adjust id lists;
- current risk-view inputs and account set;
- an exactly empty effective-diagnostic sentinel and publishable status;
- exact prefix count/end/chain binding.

On a final close, the shared transition produces the exact final closed public
row and records that `record_id` in the transaction's touched output before
evicting its domain/public continuation state. The authoritative closed row
remains in `position_lots`. Any later append targeting an evicted lot, or an
open whose derived `lot_id` collides with any stored row, is not append-safe and
selects full replay. The latter proof uses one record-id lookup, not an O(E)
closed-id set in the checkpoint.

It does not store closed-lot history, complete prefix allocations, any
diagnostic row, prior close/adjust ids, or copied raw event history. Explicit
full APIs retain those facts.

The full oracle and resumed mode both invoke the same transition primitives.
`project_trade_events()` keeps its current public `ProjectionResult` behavior,
including complete allocations/diagnostics. The internal current-publication
result is a separate narrow type and is never returned by historical APIs. The
domain transition returns deltas; the full collector retains finalized lots and
allocations, while the resumable collector emits the final touched public row
then discards finalized continuation state. This is a retention-policy
difference around one business transition, not a second projector.

### 6.6 Successful diagnostic contract

Every current event/import/invariant diagnostic produced by the source today is
an error and `ensure_projection_publishable()` rejects it. V1 therefore admits
a checkpoint only when the full-oracle effective diagnostic list is exactly
empty. The checkpoint stores only the fixed sentinel
`sha256("position_projection_diagnostics.empty.v1")` and count zero.

Any diagnostic from a tail—including a future warning—makes the checkpoint
result ineligible and selects the canonical full path. This preserves exact
writer result/error behavior without storing an O(E) diagnostic chain.
Historical APIs continue returning the complete full-replay diagnostic list.

### 6.7 Event-prefix chain

The event-prefix binding uses the exact canonical `event_json` bytes already
produced by `encode_trade_event_for_storage()` and persisted in SQLite:

```text
H0 = sha256(UTF8("position_projection_event_prefix.v1"))
B  = UTF8(event_json)
Hn = sha256(raw_bytes(Hn-1) || uint64_be(len(B)) || B)
```

Events are fed in `(trade_time_ms, event_id)` order. Hash strings are lowercase
hex only at storage boundaries; the recurrence uses the prior 32 raw bytes.
`prefix_event_count` and prefix-end sort key are checked separately. Ordinary
fast writes extend the stored chain with the strict tail and never rehash the
prefix.

### 6.8 Canonical lot fingerprint

One domain helper implements the already frozen
`position_lots_fingerprint.v1`: `record_id` plus complete `fields`, rows sorted
by record id, recursively sorted object keys, preserved list order and
missing/null distinction, UTF-8 compact JSON, and no NaN/Infinity. Writer,
reader, shadow comparator, verifier, and migration call this helper.
The repository supplies rows ordered by `record_id`; the helper exposes a
streaming hasher so writer verification does not build a second complete
decoded account graph.

## 7. Trigger and invalidation contract

The implementation contains one versioned column-classification map for every
column returned by `PRAGMA table_info(trade_events)` and
`PRAGMA table_info(position_lots)`. Each column is classified as
projection-affecting, metadata-only, or integrity/identity. Migration and tests
fail on an unclassified column. Trigger SQL is generated/reviewed from this
closed map; adding a column requires an explicit classification and schema
version decision.

### Source generation

- trade-event INSERT/DELETE increments global `source_generation`;
- UPDATE increments and invalidates when `event_id`, normalized `account`,
  `event_json`, or `trade_time_ms` actually differs;
- timestamp-only UPDATE does not increment;
- an idempotent same-id/same-payload upsert performs no DML and no increment;
- a conflicting same-id event aborts with no state change.
- trigger tests cover INSERT, projection-affecting and metadata-only UPDATE,
  DELETE, and SQLite REPLACE behavior; new code never uses REPLACE for canonical
  event idempotency.

### Lot generation

- meaningful lot INSERT/DELETE increments the affected account's
  `lots_generation`;
- UPDATE increments only for changes to `record_id`, account, fields JSON,
  source event, or indexed contract scalars; `updated_at_ms` alone is excluded;
- an account move increments both old and new account generations;
- triggers derive the mixed-version account from JSON only if the normalized
  column is null, and abort on conflict/missing scope.

### Checkpoint invalidation

V1 is deliberately conservative:

| Mutation | Checkpoint decision |
|---|---|
| strict non-control insert after a checkpoint boundary | checkpoint remains eligible; event is in tail |
| insert whose sort key is at/before a checkpoint boundary | invalidate every intersected/later checkpoint |
| `void` or `repair` insert | invalidate all checkpoints; use full replay |
| controlled event UPDATE or DELETE | invalidate all checkpoints; use full replay |
| unclassified/direct mutation | invalidate all checkpoints; use full replay |
| projector schema/implementation change or payload/hash failure | mark checkpoint mode untrusted; tail prohibited |

There are at most three checkpoint boundary rows. A backdated insert compares
its `(trade_time_ms, event_id)` only with those K rows and invalidates every row
whose boundary is at or after that key. A control/update/delete invalidates all
K rows directly. Invalidation never scans event history or discovers affected
lots, so its lookup cost is O(K), K<=3.

Ordinary semantic invalidation leaves a previously enabled global mode enabled.
The owning writer immediately uses full replay and, after successful exact
publication, atomically seeds a new `full_oracle` checkpoint. Corruption,
schema-cookie or loaded-implementation mismatch, or full/tail shadow mismatch
changes the global mode to `untrusted`; that state alone needs explicit
verify/reactivate.

The v1 plan intentionally does not build a dependency graph to recover an
earlier checkpoint for rare control mutations. Full replay is the minimal safe
fallback allowed by the Phase 1 contract. This choice can be revisited only if
measured repair/void frequency justifies it.

## 8. Projection and publication state machine

### 8.1 Full mode

Used when checkpoint mode is disabled/untrusted, no trusted checkpoint exists,
the mutation is not strict append-safe, the schema/payload binding fails, or an
explicit caller requires historical output.

1. Read canonical events once in `(trade_time_ms, event_id)` order.
2. Run the full oracle through the shared transition primitives.
3. Require publishability.
4. Diff exact desired/current public lots by `record_id`.
5. Apply only added/changed/removed rows.
6. Capture final source and lot generations and publish all account heads.
7. When the full path was selected because of normal semantic invalidation, or
   when invoked by migration/rebuild/full verifier, seed/replace a
   `full_oracle` checkpoint from active resumable state in the same transaction
   if and only if the exact effective diagnostic list is empty. Normal
   invalidation preserves enabled mode; disabled/untrusted mode never becomes
   enabled implicitly.

There is no second replay after commit.

### 8.2 Fast append mode

Preconditions, all required:

- checkpoint mode is enabled;
- newest selected checkpoint is trusted, schema-valid, hash-valid, and not
  invalidated;
- the cached loaded-projector implementation fingerprint equals source state,
  head, checkpoint, and activated acceptance evidence exactly;
- every candidate event is canonical and its mutation class is append-safe;
- no candidate is `void`/`repair`;
- ordered tail query plus candidate events contains no prefix intersection or
  unproven dependency;
- normalized-account readiness and head invariants hold.
- the current SQLite schema cookie exactly matches the activated/checked
  source-state and checkpoint bindings;
- the checkpoint has the zero-diagnostic sentinel;
- every target lot is present in active resumable state, and a derived new
  `lot_id` has no stored-row collision.

Steps inside one `BEGIN IMMEDIATE` writer transaction:

1. Upsert canonical events; triggers own source generation/invalidation.
2. Re-read final mutation/source metadata and select/validate the checkpoint.
3. Read only the ordered strict suffix after its boundary; candidate
   idempotency is checked separately by primary-key event-id lookup.
4. Decode the accumulator and apply the suffix through the shared domain
   transitions and stateful publisher.
5. Require exact zero diagnostics and publishability; an unsafe/mismatched tail
   result is never published.
6. Apply DML only for the accumulator's touched `record_id` set, verifying each
   desired row against current SQLite state.
7. Capture final global source generation and final per-account lot generations.
8. Publish heads for all known accounts; unchanged accounts reuse their trusted
   lot fingerprint/generation, changed accounts receive recomputed values.
9. If the 100-event/1-MiB cadence is reached, insert one derived checkpoint and
   prune to K<=3; otherwise write zero checkpoint payload bytes.
10. Commit all event/lot/head/checkpoint changes together.

Before step 1, the runtime reads only the submitted event ids. If every id
already exists, it first folds the stored event's immutable
`raw_payload.cash_conversions` into the incoming canonical event exactly as the
current writer does, then compares the resulting persisted canonical bytes. An
exact match returns the existing public result contract with no replay,
generation change, lot/head DML, or checkpoint rotation; any other same-id
payload is an immutable conflict and rolls back. A mixed batch reuses stored
conversions for existing ids, attaches new conversions only to new ids, and
continues with only the newly created events; the strict tail query remains the
single ordering authority.

If checkpoint corruption or semantic inconsistency is detected before DML, the
tail result is discarded. The transaction may use one canonical full replay
and publish that result, while setting checkpoint mode `untrusted`; if full
replay is invalid, the transaction rolls back. Current lots/heads may be exact
and trusted while checkpoint mode remains untrusted. It never publishes both
paths.

### 8.3 Head publication

The account set is:

```text
existing projection heads
union checkpoint accounts
union accounts affected by tail events
union old/new accounts of touched lots
```

This preserves zero-lot accounts without scanning event history. Every head
captures the same final global source generation. Only physically changed
accounts advance lot generation. Head publication occurs after lot DML and
before commit.

Each head also stores its exact retained `lot_count`. Fast diff publication
updates changed-account counts from the prior head plus added/removed rows; the
existing global `position_lot_count` writer result is the O(A) sum of final head
counts rather than a table-wide `COUNT(*)` or decoded lot list.

Required cross-account proof: an append for account A publishes the final
global source generation into the already-known head for account B without
rewriting B lots/fingerprint; a same-transaction trusted read of B and the next
transaction's public read both remain fresh. The fanout benchmark measures this
O(A) head write explicitly.

### 8.4 Trusted read

`read_current_position_projection(account)` uses one SQLite read transaction:

1. normalize/reject account;
2. read singleton source state and account head, and require their exact cached
   loaded-projector implementation fingerprint;
3. require trusted status and exact `source_generation == built_source_generation`;
4. require exact `lots_generation == built_lots_generation`;
5. read only indexed account lots, including active and closed rows retained by
   the canonical projection;
6. recompute the shared fingerprint and require equality;
7. return trusted rows/head metadata or structured `data_unavailable`.

Absent schema/head during the declared migration window returns
`not_initialized`; it does not perform hidden replay. Once a head exists, any
mismatch fails closed without fallback/write. Consumer adapters may continue
their legacy path only before the explicit cutover gate.

## 9. Preflight and writer-facade contract

### Preflight

Append-safe open/close/adjust preflight uses a trusted current accumulator plus
the candidate event in memory. It returns the source generation/checkpoint id
used as a diagnostic token. The later write transaction never trusts that token
without re-reading current state; concurrent drift causes recomputation or
safe fallback, not publication from stale preflight.

Untargeted close/FIFO resolution uses active resumable lots filtered by the
selector and sorted by `(opened_at_ms, record_id)`. Explicit record-id targets
use one indexed lot lookup and must also be present in active state for fast
mode. This removes the current all-`position_lots` scan from eligible close
preflight without changing FIFO semantics.

Void/repair preflight remains full oracle because it changes prefix semantics.
Preflight remains read-only.

### Facade inventory

All event-writing call sites are routed through the common transactional
runtime. Mode is explicit:

| Call path | Phase 3A mode |
|---|---|
| `persist_trade_event_object` for single open/close/expire/assignment/exercise/adjust/verification | fast when every guard passes; otherwise full |
| `persist_manual_adjust_events` | fast only when the complete batch is strict append-safe; otherwise full |
| `rebuild_position_lots_from_trade_events` / bootstrap | force full; may seed full checkpoint |
| manual void/repair and intervention repair | force full |
| lifecycle allocation with correction/control events | force full in v1 |
| ordinary single/batch events carrying Combo Yield/SP+LC strategy metadata only | fast when every ordinary append guard passes; strategy labels never force full |
| second-leg Combo identity transaction, adoption, or reconciliation that mutates/validates broader membership | force full in v1 |
| explicit allocations, audit, preview, projection verification | force full/read-only as appropriate |

Even force-full writers use diff DML and publish generations/heads atomically.
No migrated writer calls global `replace_position_lots` as an unowned side
effect. Repository-level direct lot mutation remains possible only for explicit
repair/migration and leaves heads detectably mismatched unless it publishes
through the runtime.

An `rg`-backed inventory test/gate must account for every call to
`project_stored_trade_events_to_position_lots`, `project_trade_events`,
`replace_position_lots`, and direct trade-event DML under `src/application/ledger`.
For every facade classified fast, fail-on-call spies forbid
`list_trade_events()`, `list_position_lots()`, full projector entrypoints, and
global lot replacement. Candidate idempotency must call
`get_trade_events_by_ids(event_ids)` once and may not create an `existing_by_id`
map from the full ledger. Tests freeze stored-cash-conversion retry parity,
same-id conflict behavior, and mixed existing/new batch ordering: existing
events are not folded again and cause no generation/head/rotation change; only
new strict-tail events produce DML.

## 10. Migration, activation, mixed version, and rollback

### 10.1 Startup behavior

Repository initialization creates additive metadata-only columns/tables and
trigger objects. It may create new indexes automatically only when the owning
table is empty/new. On a populated store, missing Phase 3A indexes make
readiness false and checkpoint mode remains disabled until explicit migration
apply builds them.

Initialization must also stop running the existing
`_backfill_position_lot_contract_columns()` full-table compatibility pass on
every open. Its one-time inventory/backfill/index work moves to explicit apply;
after migration, startup validates readiness from schema metadata and bounded
state only. Startup performs no full event/lot scan, replay, checkpoint build,
or row rewrite.

### 10.2 Explicit migration workflow

Add an operator facade under `./om option-positions projection-migration`:

1. `inventory` is read-only and emits a versioned manifest containing store
   identity, projector schema, loaded-projector implementation fingerprint,
   SQLite schema cookie, counts, canonical account mappings, account/scalar
   null/conflict counts, event/lot fingerprints, required-index inventory,
   estimated checkpoint bytes, and readiness.
2. `apply --manifest ...` requires existing local-write confirmation, rechecks
   the exact store/fingerprints under `BEGIN IMMEDIATE`, backfills normalized
   accounts and legacy contract scalars, builds required populated-table
   indexes with measured wall/CPU/WAL, runs one full oracle, applies lot diff,
   publishes trusted heads, seeds one full-oracle checkpoint, and leaves
   checkpoint mode `disabled`.
3. `verify` is read-only and checks columns/indexes/triggers, account equality,
   generations, heads, current fingerprints, checkpoint integrity/K bound, and
   full-oracle parity.
4. `activate --acceptance-manifest ... --shadow-manifest ...` is a separate
   high-risk local write. It requires both versioned artifacts to be `pass`,
   match the projector schema, exact loaded-projector implementation
   fingerprint, source commit recorded by the acceptance run, SQLite schema
   cookie, and exact current source generation/fingerprint, and have no
   unresolved resource or parity failure. It changes only checkpoint mode from
   `disabled` to `enabled`.
5. `deactivate` is the recovery/control-plane inverse for measured performance
   degradation or operator choice. It requires the existing local-write
   confirmation, changes only `enabled -> disabled`, and never deletes events,
   lots, heads, or checkpoint rows.

Implementation and temp-database tests may exercise all transitions. This
Gateflow work unit does not run `apply` or `activate` on a live ledger.

### 10.3 Shadow evidence

Extend the explicit verification surface with a runtime-shadow mode. By
default it is read-only and compares:

- full oracle versus checkpoint/tail current lots/risk/public records;
- publishability and the exact empty successful diagnostic list/sentinel;
- current SQLite lots/head fingerprints;
- source/checkpoint prefix bindings;
- expected DML row set without applying it.

Persisting evidence remains an explicit flag and does not activate the fast
path.

### 10.4 Mixed-version behavior

- Old binaries may ignore new tables/columns.
- Old inserts with null normalized account are tolerated only when canonical
  JSON contains one lowercase account; new readiness becomes false until
  backfill.
- Old global lot replacement advances lot generations through triggers but
  cannot advance built heads, so new trusted reads fail closed rather than
  trusting the old publication.
- Old startup against a populated store cannot create the new normalized
  indexes or activate checkpoints; new readiness remains false until explicit
  migration apply.
- New code on an unprepared store stays full-only and preserves existing public
  writes; checkpoint use is impossible until explicit activation.
- A new binary/checkout whose loaded-projector fingerprint differs from the
  activated source state makes checkpoint mode untrusted before tail use.
  Full-oracle publication remains available, but reactivation requires new
  acceptance evidence for the loaded implementation.
- A rolled-back binary similarly sees a fingerprint mismatch and stays
  full-only; additive tables require no destructive down-migration.
- Rolling back source code does not require dropping tables or rewriting
  events/lots. A later new reader sees generation drift and requires explicit
  full rebuild/verify.

## 11. Concurrency, failure, and recovery invariants

- All event/lot/head/checkpoint publication uses the existing same-host
  `BEGIN IMMEDIATE` SQLite authority.
- An idempotent retry with identical event id/payload causes no source
  generation, lot DML, head rewrite, or checkpoint rotation.
- Any exception before commit rolls back event, generation, lot diff, head, and
  checkpoint changes together.
- Crash tests inject before/after event insert, tail projection, each lot-DML
  class, head capture, checkpoint insert/prune, and commit.
- Payload hash/schema failure marks/returns checkpoint unusable before tail DML.
- A shadow/full mismatch always makes checkpoint mode untrusted. If no canonical
  full publication occurs in the same transaction, affected heads remain or
  become untrusted and readers fail closed. If the writer successfully runs and
  publishes the canonical full oracle, current heads may be trusted while only
  checkpoint mode remains untrusted.
- A normal invalidation is not corruption: an enabled writer performs one full
  replay, atomically publishes it, seeds a new full checkpoint, and remains
  enabled. Disabled mode remains disabled. Only corruption/schema-cookie or
  semantic-shadow mismatch becomes untrusted.
- SQLite busy/lock errors preserve current retry/error behavior; no new queue or
  hidden retry loop is introduced.
- Existing explicit full rebuild is the universal recovery path.

## 12. Public and internal interface decisions

Public behavior preserved:

- existing add/close/adjust/void/repair/rebuild commands and result fields;
- existing `replace_position_lots()` return meaning where compatibility callers
  remain during migration;
- existing full allocation/diagnostic APIs;
- existing filesystem `projection_verify.checkpoint.json`, which remains a
  verification-result reuse cache and is not repurposed as the new resumable
  SQLite checkpoint.

New versioned surfaces:

- `read_current_position_projection(account)` application facade;
- `projection-migration inventory|apply|verify|activate|deactivate` CLI;
- runtime-shadow verification mode;
- read-only projection status payload with source/head/checkpoint mode,
  readiness, generation mismatch reasons, K/bytes, last full verification, and
  fast/full/fallback reason counters plus bounded wall/CPU/fingerprint-row-byte
  summaries in process/status artifacts. No per-event timing row is added to
  the ledger.

No new runtime config key is introduced. Activation is bound to the explicit
SQLite migration state and exact acceptance evidence, not a loosely editable
JSON flag.

## 13. Implementation slices

Five slices are intentional even though Gateflow normally prefers at most
three. This work has five independently reviewable behavioral gates whose
failure must leave later cutovers impossible: durable diff/freshness under the
unchanged full oracle (S1), shared resumable semantic parity (S2), cache/runtime
safety
with facades still full-only (S3), facade cutover/shadow parity (S4), and
operator acceptance/activation evidence (S5). Combining S1-S3 would make a
diff/publication defect indistinguishable from resumable semantic/runtime
defects;
combining S4-S5 would let benchmark/activation code land before the real public
facades are inventoried. Each slice depends on the prior accepted slice and may
not begin early.

Goal alignment is direct: S1 satisfies completion signals 5, 8, and 10; S2
satisfies 1-3; S3 satisfies 6, 7, and 12; S4 satisfies 4, 5, and 9; S5 proves
4, 6, 10, and 11 and documents the operational boundary. This implements the
accepted Phase 1 section 5.6 checkpoint route after its hard performance gate
failed; it neither changes the authority model nor pulls Phase 3B into scope.

### S1 — additive schema, generations, lot diff, fingerprints, trusted reader

**Objective:** remove global lot replacement and establish durable freshness
without enabling checkpoint reads or changing full-replay computation.

**Prerequisite / outcome:** accepted plan only. Completion yields exact
diff/head/read behavior under the unchanged full oracle with checkpoint mode
disabled.

**Allowed files:**

- `domain/domain/ledger/position_fingerprint.py` (new, narrow);
- `domain/domain/ledger/__init__.py`;
- new `src/application/ledger/position_projection_publication.py`;
- new `src/application/ledger/projector_implementation.py`;
- `src/application/ledger/repository.py` and repository protocols;
- `src/application/ledger/writer.py`, `manual_trades.py`, `interventions.py`,
  `combo_reconciliation.py`, and `bootstrap.py` only to route existing
  full-oracle publications through the shared lot-diff/head boundary;
- new focused persistence tests;
- `tests/test_ledger_sqlite_workflows.py` only for public regression coverage.

**Exact changes:**

- extend `position_projection_source_state` and heads first, but create the
  checkpoint table in S3 so S1 does not land a dormant, unowned cache schema;
- add metadata-only columns/tables/triggers in sections 6-7 without startup
  backfill; create normalized indexes automatically only for empty tables;
- retire the repeated startup contract-scalar backfill and expose explicit
  migration primitives for account/scalar backfill and populated index build;
- add the closed column-classification/schema-cookie contract and complete
  INSERT/UPDATE/DELETE/REPLACE trigger matrix;
- implement canonical fingerprint helper and streaming ordered account hashing;
- implement the closed semantic-source manifest/import-graph guard and cache
  the exact loaded implementation fingerprint once per process; a trusted S1
  head may never use a null or placeholder fingerprint;
- implement exact lot diff (`added`, `changed`, `removed`, `unchanged`, touched
  accounts) in the shared publication boundary and route every existing
  full-oracle writer publication through it;
- add `get_trade_events_by_ids()` and bounded active/record-id lot queries for
  later slices without using them to change preflight behavior yet;
- implement source/lot-generation and head primitives plus one-transaction
  trusted reader;
- leave checkpoint mode disabled.

**Tests/assertions:** complete mutation/column-classification matrix,
timestamp-only update exclusion, INSERT/UPDATE/DELETE/REPLACE behavior,
idempotent/conflicting events, account conflict/null mixed version, account
move, zero-lot account, unchanged-row zero DML, direct mutation/schema-cookie
mismatch, rollback, A-write/B-read freshness, two-account isolation, indexed
query plan, canonical streaming fingerprint edge cases, populated startup zero
history row visits/index builds, closed import-graph classification,
raw-byte/path framing golden vectors, checkout/release-root resolution, every
full-writer diff inventory, and no full-table DELETE SQL.

**Stop condition:** any full-oracle public behavior changes, an unclassified
mutation can leave a falsely fresh head, a writer bypasses diff/head
publication, or diff DML changes unchanged rows.

**Non-goals:** changing full-replay CPU, checkpoint selection/tail replay,
resumed preflight, public fast cutover, or live-store migration/activation.

### S2 — shared resumable projector and exact publisher parity

**Objective:** introduce one domain transition engine capable of full history
and resumable current state without changing existing full-oracle results.

**Prerequisite / outcome:** accepted S1. Completion yields a pure, serializable
full-or-tail semantic core; S1 schema/diff remains disabled/full-only for
checkpoint use and no writer fast cutover occurs.

**Allowed files:**

- `domain/domain/ledger/projection.py`;
- new narrow `domain/domain/ledger/projection_state.py` if separation is needed;
- `domain/domain/ledger/events.py`;
- `domain/domain/ledger/lots.py`;
- `domain/domain/ledger/economics.py`;
- `domain/domain/ledger/invariants.py`;
- `domain/domain/ledger/identity.py`;
- `domain/domain/ledger/position_fields.py`;
- `domain/domain/ledger/__init__.py`;
- `src/application/ledger/event_codec.py` only for the shared streaming import
  adapter required by full and tail collection;
- `src/application/ledger/publisher.py`;
- `tests/test_ledger_projection.py`;
- `tests/test_ledger_publisher.py`;
- new focused state/serialization test file.

**Exact changes:**

- factor event transitions out of the one-shot loop;
- add versioned active-only resumable state with zero-diagnostic sentinel;
- keep complete `close_event_ids` only in the full-oracle result collector. The
  resumable lot schema has scalar `last_event_id`/`last_close_event_id` and
  aggregate economics but no close-id vector; both collectors call the same
  economic transition functions;
- add one stateful publisher transition whose value is the exact complete
  current public fields per active lot; open initializes it, adjust folds it,
  partial close updates it, and final close emits then evicts it;
- keep `project_trade_events()` and
  `project_stored_trade_events_to_position_lots()` full results byte/field
  compatible;
- prove serialization round-trip and full-then-tail equality;
- prove repeated partial closes do not copy complete close-id history and fully
  closed-lot count does not change checkpoint payload size. The serialized
  resumable domain-lot schema must have no `close_event_ids` member, while
  emitted public rows preserve the existing scalar `last_close_event_id`;
- prove an event targeting an evicted closed lot is classified force-full;
- prohibit SQLite/application imports in domain.

**Tests/assertions:** existing projection/publisher suite unchanged; open,
partial/multiple/final close, adjust, verification, invalid event, fee
remainder, strategy patch, multi-account, and deterministic-order fixtures
compare full and resumed active economics/risk/touched public rows exactly.
Seeded deterministic property-style sequences fork at every valid prefix and
compare full versus resume+tail. Retained-closed fixtures assert checkpoint
bytes are invariant when only lifetime closed-pair count grows. Complete full
allocations/diagnostics stay unchanged, and any nonempty diagnostic makes the
fast result ineligible.

**Stop condition:** any full-oracle behavior change or unbounded resumable field
blocks the slice.

**Non-goals:** checkpoint persistence/selection, writer routing to fast mode,
migration, or activation.

### S3 — checkpoint store, strict-tail runtime, invalidation, cadence

**Objective:** add the bounded cache and common transactional projection runtime
while all external writers remain full-only.

**Prerequisite / outcome:** accepted S2. Completion yields a test-only/internal
fast-if-safe runtime and bounded cache; production/public facades remain on the
full oracle.

**Allowed files:**

- new `src/application/ledger/position_projection_runtime.py`;
- existing S1 `src/application/ledger/projector_implementation.py` only when
  the semantic manifest/digest must be updated for accepted S2 source changes;
- `src/application/ledger/repository.py` persistence primitives;
- `src/application/ledger/publisher.py` serialization adapter only as required;
- new runtime/checkpoint tests.

**Exact changes:**

- implement checkpoint encode/decode/hash/binding, selection, tail query,
  invalidation, full fallback, head publication, rotation, pruning, and result
  telemetry;
- bind checkpoints and runtime eligibility to the S1 one-time loaded-source
  fingerprint cache; ordinary runtime operations must not read or hash files;
- implement the exact length-framed event-prefix chain and O(K<=3) backdate
  invalidation algorithm;
- create checkpoints only at the frozen cadence or explicit full seed/verify;
- expose forced-full and fast-if-safe internal entrypoints;
- keep checkpoint mode disabled by default; tests enable it explicitly;
- no writer facade cutover yet.

**Tests/assertions:** strict append, insertion within tail, backdate into prefix,
  void/repair/update/delete/unclassified/schema/implementation invalidation,
  corrupted payload,
  derived/full pruning K<=3, no-rotation zero checkpoint writes, 100-event and
  1-MiB rotation, crash injection, stale parent metadata, full fallback parity,
  normal-invalidation automatic full-checkpoint recovery, corruption remains
  untrusted, no full-prefix calls, exact account-head capture after lot DML,
  event-chain golden vectors, semantic-file-change-without-manifest-update
  failure, new first-party import-without-classification failure,
  accepted/different loaded-source fingerprints, checkout-root and
  release-root resolution, missing/unreadable source fail-closed, and zero
  source-file reads after process initialization.

**Stop condition:** unsafe tail selection, non-atomic head/checkpoint
publication, or checkpoint/WAL/state bounds are not testable.

**Non-goals:** external writer cutover, migration activation, or Phase 3B.

### S4 — preflight and writer-facade integration plus shadow oracle

**Objective:** remove duplicate full replay from eligible end-to-end operations
and route every writer through one owned publication boundary.

**Prerequisite / outcome:** accepted S3. Completion yields fully inventoried
public write routing plus shadow parity, but mode remains disabled unless S5
evidence later accepts it.

**Allowed files:**

- `src/application/ledger/position_projection_runtime.py`, limited to exposing
  the already-owned in-transaction runtime and preserving projection
  diagnostics for facade result compatibility;
- `src/application/ledger/preflight.py`;
- `src/application/ledger/lot_resolver.py`;
- `src/application/ledger/writer.py`;
- `src/application/ledger/manual_trades.py`;
- `src/application/ledger/interventions.py`;
- `src/application/ledger/combo_reconciliation.py`;
- `src/application/ledger/bootstrap.py`;
- `src/application/ledger/commands.py`;
- `src/application/ledger/api.py`;
- corresponding existing ledger/CLI tests and one facade-inventory test.

**Exact changes:**

- expose one application-internal runtime entrypoint for callers that already
  hold the SQLite transaction; it must reuse the S3 implementation, require an
  active transaction, preserve input-order idempotency flags, and never commit
  or roll back the caller-owned transaction;
- preserve the full oracle's diagnostic contract: successful full, unchanged,
  and checkpoint-backed results return the exact empty diagnostic list, while
  every current diagnostic remains an error and fails before publication;
- use resumed preflight for eligible candidates and retain full control preflight;
- route the facade matrix in section 9 through the runtime with explicit mode;
- replace candidate `existing_by_id` full-history maps with one primary-key
  batch lookup and route eligible FIFO close resolution through active state;
- preserve public result/error/idempotency contracts;
- add read-only full-vs-tail shadow comparison and bounded mismatch samples;
- ensure no eligible transaction executes preflight full replay plus writer full
  replay;
- ensure forced-full paths still use lot diff/generation/head publication.

**Tests/assertions:** every facade classification, stale preflight token,
  idempotent retry, batch atomicity, lifecycle terminal/control fallback, combo
  metadata ordinary-fast parity, special combo-identity/reconciliation
  fallback, rebuild/bootstrap recovery, error parity, full-prefix spy failures
  and full-lot-list spy failures on fast paths, explicit target-to-closed-lot
  fallback, account-crossing rejection, caller-owned transaction rollback,
  exact input-order created flags, successful empty-diagnostic and error
  parity, and `rg` inventory closure.

**Stop condition:** any event writer bypasses heads/diff publication or an
existing public workflow changes semantics.

**Non-goals:** declaring readiness, live migration/activation, or optimizing
special control/identity membership transactions.

### S5 — explicit migration, acceptance/activation gate, benchmarks, docs

**Objective:** make cutover measurable and operable without performing it on a
live store.

**Prerequisite / outcome:** accepted S4. Completion yields reproducible
acceptance/readiness artifacts and safe operator facades; it performs no live
apply/activate action.

**Allowed files:**

- new `src/application/ledger/position_projection_migration.py`;
- `src/application/ledger/projection_verify.py`;
- `src/interfaces/cli/option_positions.py`;
- `src/application/research/performance_baseline.py`;
- `tests/test_option_positions_cli.py`;
- `tests/test_research_performance_baseline.py`;
- focused migration/verification tests;
- `docs/AGENT_WIKI.md`;
- Gateflow artifacts for this work unit.

**Exact changes:**

- implement inventory/apply/verify/activate contracts from section 10;
- implement the bounded deactivate transition and status reason/timing
  summaries from sections 10 and 12;
- extend the existing deterministic benchmark rather than create a second
  harness;
- measure explicit full rebuild separately, plus old full-mode and new
  fast-mode executions of the same real single and atomic-batch append facades
  from independently reset identical fixtures;
- include ordinary single and atomic-batch candidates carrying
  `combo_yield`/SP+LC strategy metadata in the comparable fast-facade set;
  separately measure only the special identity/membership transaction as
  force-full;
- measure checkpoint no-rotation, forced 100-event rotation, forced 1-MiB
  rotation, fast preflight+writer, allocation, history/lot reader call counts,
  SQL row counts, checkpoint bytes, changed-account fingerprint-only cost,
  `retained_lots_10x` diagnostic capacity, loaded-projector fingerprint startup
  cost, index-migration cost, and WAL;
- record `retained_lots_10x_guarantee=false` in the acceptance manifest and
  emit a capacity warning rather than converting that diagnostic into an
  undeclared activation gate;
- report every force-full facade with its reason and no-regression metrics; do
  not roll it into the fast-path pass rate;
- generate one versioned Phase 3A acceptance manifest that fails closed on any
  missing/noncomparable result;
- document exact dry-run/read-only commands and the separate authority needed
  for live apply/activate.

**Tests/assertions:** stale/wrong-store manifest rejection, apply rollback,
  verify mismatch, activation evidence/projector/implementation-fingerprint/
  schema-cookie/generation binding, no-write inventory/shadow, exact 5/30
  artifact validation,
  independently reset fixture identity, reference-host comparison, both
  rotation latency gates, K/space/WAL/allocation/call-count gates, and CLI
  safety flags.

**Stop condition:** activation can occur without exact evidence/current-store
binding, or the reference acceptance decision is not `pass`.

**Non-goals:** production apply/activate, release, deploy, timer installation,
notification, broker write, or data deletion.

## 14. Validation commands and expected results

Focused commands will be finalized against the implemented test filenames, but
the accepted slices must run at least:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_ledger_projection.py \
  tests/test_ledger_publisher.py \
  tests/test_ledger_sqlite_workflows.py \
  tests/test_option_positions_cli.py \
  tests/test_research_performance_baseline.py
```

plus every new Phase 3A focused test module.

Aggregate ledger/research gate:

```bash
./.venv/bin/python -m pytest -q tests/test_ledger_*.py tests/test_research*.py
```

Static/hygiene gates:

```bash
./.venv/bin/python -m ruff check domain/domain/ledger src/application/ledger \
  src/application/research/performance_baseline.py src/interfaces/cli/option_positions.py
./.venv/bin/python -m compileall -q domain/domain/ledger src/application/ledger \
  src/application/research src/interfaces/cli
git diff --check
```

Reference acceptance uses the existing harness with 5 warmups / 30 measured
repetitions and exact host fingerprint. Timing runs exclude cProfile and
tracemalloc; separate diagnostic artifacts explain CPU and allocation.

Expected final assertions:

- all correctness tests pass;
- full-oracle historical outputs remain compatible;
- full/tail current equivalence and the successful empty diagnostic result are
  exact;
- same-facade/same-candidate baseline and fast measurements are comparable;
- explicit rebuild and force-full facade costs are reported separately;
- all frozen time/space/row/call-count gates pass;
- `lot_diff_publication=pass`, `checkpoint_tail=pass`, and combined Phase 3A
  readiness=`ready` only when both pass;
- otherwise readiness remains `not_ready` and activation rejects.

## 15. Documentation decision

Update `docs/AGENT_WIKI.md` with:

- authority versus cache distinction;
- full versus checkpoint/tail operations;
- migration inventory/apply/verify/activate commands and safety boundaries;
- trusted-read mismatch meanings and recovery;
- checkpoint cadence/K/space rules;
- benchmark invocation and interpretation;
- explicit statement that Strategy Lab/backtest/full allocation APIs still use
  canonical full history;
- explicit statement that source merge does not authorize live migration,
  activation, release, or deployment.

No general architecture platform document or new configuration guide is
created.

## 16. Risks, residual risks, and tracking destinations

| Risk | Control / destination |
|---|---|
| Checkpoint decode is O(active resumable lots), while `position_lots` still retains closed rows. | Final-close emit-and-evict contract; retained-closed history-growth fixture must leave checkpoint bytes unchanged. |
| Exact frozen account fingerprint still streams retained public rows for each changed account. | Report fingerprint-only scaling, bound allocation, and enforce the combined 500-ms activation gate. Gate failure leaves readiness `not_ready`; post-activation degradation is visible in status and permits explicit deactivation. Changing the frozen fingerprint or adding a Merkle projection requires a later measured planreview. |
| Full fallback for void/repair/special Combo identity-membership operations remains O(E). | Strategy metadata alone remains ordinary-fast; only explicitly inventoried broader-relationship transactions are force-full and measured. Future optimization requires observed frequency/cost. |
| Global source generation updates all A account heads. | Existing account-fanout benchmark; failure returns to per-account partition planreview. |
| Every 100-event checkpoint rotation rewrites O(S). | Separate rotation timing/WAL/space gate; zero checkpoint writes between rotations. |
| SQLite JSON trigger expressions and old binaries create mixed-version risk. | Direct-SQL/old-writer tests, null/conflict readiness, exact migration manifests, fail-closed heads. |
| Existing verification checkpoint name may confuse operators. | Preserve it but label filesystem verification cache versus SQLite resumable checkpoint in CLI/docs/status. |
| Checkpoint/full semantic drift after later projector changes. | Projector schema plus checked-in closed-source implementation fingerprint bind activation and every fast/read decision. Source hashing occurs once at process initialization, not per transaction. |
| V1 allows fast mode only for an exactly empty effective diagnostic list; a future common warning can reduce fast-path liveness. | Correctness-preserving full fallback plus explicit fast/full reason telemetry. A warning-aware bounded diagnostic state requires later measured design; warnings are not silently ignored. |
| Trusted reads include retained closed rows even though checkpoints contain active continuation only. | Reader always queries authoritative indexed `position_lots` and fingerprints all returned account rows; an exact final-close trusted-read regression freezes this behavior. |
| Scheduled full verifier is not deployed by source implementation alone. | Command/status/docs in S5; actual timer/service change is a separately authorized operational action. |
| Same-host SQLite transaction does not coordinate an external direct writer. | Generation/invalidation triggers make it detectably dirty; cross-host write serialization remains outside this work unit. |

Every residual risk is classified as controlled in this work unit or assigned
to the named later/operational boundary. No unclassified risk is accepted.

## 17. Why this plan avoids overengineering and overcoupling

- The checkpoint is justified by a measured hard-gate failure, not anticipated
  scale.
- V1 invalidates all checkpoints for rare control mutations instead of adding a
  dependency graph or per-event undo log.
- V1 admits only zero-diagnostic checkpoints instead of inventing a second
  durable diagnostic history.
- A compact checked-in semantic-source manifest closes loaded-code drift with
  one startup hash and O(1) transaction checks; it does not add a package,
  plugin, build service, or per-write source scan.
- One domain transition implementation serves full and tail modes; the
  application owns orchestration only.
- Existing SQLite, CLI, benchmark, verifier, and transaction boundaries are
  extended rather than replaced.
- Checkpoint cadence is a fixed measured constant, not a tuning subsystem.
- Phase 3B, lifecycle/research retention, scheduler deployment, and deletion
  remain independent work units.
- Force-full facades remain correct and explicit; this plan does not broaden
  scope merely to claim every operation is incremental.

## 18. Open questions

No blocking user-choice question remains after the accepted goal boundary.

The re-review must specifically try to falsify:

1. whether the resumable payload is truly sufficient and bounded;
2. whether zero-diagnostic gating plus exact active public fields preserves
   public-record parity without prefix history;
3. whether preflight or any writer facade still hides O(E) work;
4. whether the 100-event/1-MiB cadence creates unacceptable rotation spikes;
5. whether invalidation and mixed-version triggers can ever leave a falsely
   fresh head;
6. whether activation evidence binds strongly enough to the live store;
7. whether a simpler credible solution meets the already failed performance
   gate.
8. whether ordinary Combo metadata stays eligible for the same fast path while
   only special identity/membership topology remains force-full;
9. whether implementation-fingerprint checks cover every projection-semantic
   source without adding transaction-time file I/O;
10. whether flat retained-row fingerprinting passes the accepted fixture gate
    and exposes its later capacity boundary without pretending to be O(1).

## 19. Completion report format

Final closeout will report:

- accepted plan/review artifact paths and commit hashes;
- implemented slices and changed ownership boundaries;
- correctness and aggregate test counts;
- exact reference-host wall/CPU/allocation/WAL/checkpoint results;
- full/tail/diff/readiness decisions;
- migration/activation status explicitly stating live actions not performed;
- findings and final statuses;
- remaining risks with owners;
- Draft PR URL and next post-merge entry point.

## 20. Next Gateflow entry point

Create the protected accepted-plan commit containing the goal, plan, first
review, fix, final re-review, and acceptance artifact. The next code entry point
is S1 only: additive schema/generation/head contracts, diff publication,
loaded-source fingerprint, and trusted reads under the unchanged full oracle
with checkpoint mode disabled.
