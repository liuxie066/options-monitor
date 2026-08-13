# Phase 1 Contract — data-storage-and-runtime-projection.v1

## Status

- Status: amended after second plan review; reviewed full-replay path remains
  valid, while conditional checkpoint/tiering activation requires the focused
  gates below
- Source baseline: `main@421591dd`
- Supersedes: the informal Phase 1 section reviewed in `docs/reviews/plan-review-20260813-092047.md`
- Prior review artifact: `docs/reviews/plan-review-20260813-103939.md` (`pass-with-risks` before the checkpoint, research-tier, and dirty-once-publish-once amendments below)
- Scope of this document: freeze authority, state-machine, migration, and performance contracts before implementation
- This phase is design and measurement only. It does not change schemas, runtime data, retention, services, or production configuration.

## 1. Goal and success condition

The project has two histories with different jobs:

1. `trade_events` is durable trading history. It explains what changed the option position and remains the authority from which `position_lots` can be rebuilt.
2. scan/lifecycle observations record what the system saw and decided at a run or broker-check boundary. Most consumers need current state or a sealed historical input/output, not every repeated large payload.

The optimization must remove full-history work from ordinary scan and quality paths without weakening audit, replay, account isolation, or Strategy Lab evidence.

Phase 1 succeeds only when:

- every authority and generation has one named owner;
- the hot-path and offline-path complexity boundaries below are testable;
- lifecycle repeated-observation storage has a bounded per-check cost;
- run-artifact deletion cannot orphan protected research or replay inputs;
- CSV/base64 consumers have an explicit cutover gate;
- benchmark fixtures, commands, units, and pass/fail thresholds are fixed;
- no implementation agent must choose a new persistence authority or recovery state machine.

## 2. Non-goals and retained boundaries

- Do not rewrite, compact, migrate, or delete existing `trade_lifecycle_evidence` rows in Phases 1–7.
- Do not replace `trade_events` as the trading authority.
- Do not introduce a general per-event delta position projector in the default
  path. Low-frequency writes first use deterministic full replay; the bounded
  checkpoint-and-tail path in section 5.6 is activated only by its measured
  gate and requires focused re-review before implementation.
- Do not repair a projection from inside a scan or ordinary quality check.
- Do not merge the SQLite lifecycle receipt store with the filesystem scan blob store.
- Do not claim that broker cash, broker positions, and SQLite facts are atomically observed.
- Do not let Strategy Lab write runtime config, ledger/trade state, notification state, broker state, or scheduler state.
- Do not activate a cold-storage backend, evict a protected local blob, or
  delete a research artifact merely because a capacity threshold fired. The
  thresholds produce status and a preview; movement or deletion needs separate
  approval.
- Data deletion remains a separate, explicitly authorized phase. A successful migration preview is not deletion authorization.

## 3. Terms and authority map

| Term | Exact meaning | Authority / owner |
|---|---|---|
| provider attempt | A provider call was actually made. Static blocking is not an attempt. A post-call stale-generation outcome is still an attempted call but is not a valid observation. | compact lifecycle attempt audit sidecar |
| valid observation | A provider attempt returned a payload that can be projected and admitted by `settlement_observation_semantic.v1` or its successor. | admitted evidence plus compact audit sidecar |
| admitted evidence | The first valid observation or a later business-semantic change. | existing `trade_lifecycle_evidence` and settlement admission head |
| duplicate observation | A valid observation whose semantic schema and fingerprint equal the current admission head. | compact audit sidecar; it must not add admitted evidence |
| operational retry state | Current attempt count, backoff, claim, and next-attempt data. It is overwriteable control-plane state. | existing trade-inbox settlement-attempt state; never audit authority |
| position source generation | Global generation of canonical `trade_events` mutations that may affect the deterministic projection. | SQLite triggers on the authority table |
| position lot generation | Per-account generation of physical `position_lots` mutations. | SQLite triggers on the projection table |
| position projection head | The generations and fingerprint last published by a successful projector transaction. | position projector, at the end of the same transaction |
| position projection checkpoint | A bounded, versioned, verified cache of a resumable projector accumulator for an exact event prefix. It is never trading authority. | position projector / explicit integrity verifier |
| lifecycle evidence revision | Per-case invalidation revision for admitted lifecycle evidence. | existing evidence revision triggers |
| decision-input dirty state | One per-account source generation changed only by the closed set of decision-bearing source tables; dirty is derived from source generation != projection built generation and is not a second stored boolean. | narrow SQLite triggers; publisher captures the final generation only after one successful transaction-end publish |
| current decision projection | Per-account materialized facts needed by ordinary position/lifecycle/combo readers, derived from trusted current lots and canonical lifecycle/combo inputs. It is a cache/projection, never audit authority. | decision projection publisher |
| research dataset generation | An immutable manifest generation that binds exact partition/blob hashes and provenance; experiments bind this id rather than copying a dataset. | Research/Shadow Replay manifest writer |
| run seal | Integrity and run-binding hash over a manifest. | run artifact writer |
| coherence | Descriptive timing relationship among independently observed source sections. | runtime snapshot builder; not an atomicity claim |

The three revision domains—position projection, lifecycle evidence, and external broker observation—must not share a counter or be inferred from one another.

## 4. Frozen decision A — preserve admitted evidence; add only a compact audit sidecar

### 4.1 Authority boundary

`trade_lifecycle_evidence` remains the logical and physical authority for admitted semantic evidence. Existing foreign keys from admission heads, source consumptions, and allocations continue to reference it. Existing rows are immutable, but the table continues accepting a new row whenever settlement semantics change.

The sidecar records provider attempts and duplicate observations. It does not become a second evidence authority and is not an input to allocation or position projection.

### 4.2 Canonical receipt and hash

For a valid observation, the receipt payload is exactly the complete
`evidence["observation"]` mapping produced after
`attach_settlement_semantics()`.  This includes the versioned semantic
projection and fingerprint that are actually admitted, but excludes the outer
evidence envelope, generated evidence id, and database timestamps.  Both the
first-evidence reader and the sidecar writer call one shared canonicalizer;
neither reconstructs the receipt from selected fields.  `receipt_sha256` is
calculated over its UTF-8 canonical JSON:

```text
json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

The hash preimage includes a receipt-canonicalization schema identifier. “Raw receipt” in this contract means this canonical logical JSON, because the current application persists parsed JSON rather than the provider's original wire bytes.

For provider failures, the audit row stores a small normalized outcome code and a hash of redacted diagnostic classification; it does not store raw exception text, request credentials, or provider payload bytes.

### 4.3 Target schema contract for Phase 2

Names may change mechanically during implementation, but cardinality, keys, and ownership may not.

`trade_lifecycle_attempt_audit_heads` — one mutable row per lifecycle case:

- `audit_case_key` integer primary key plus `case_id` unique foreign key to `trade_lifecycle_cases`;
- `last_ordinal` non-negative integer;
- `chain_sha256` 32-byte BLOB;
- `current_span_ordinal` nullable;
- `last_invocation_id` nullable 16-byte BLOB;
- `updated_at_ms`.

`trade_lifecycle_attempt_audits` — one compact append-only row per provider attempt:

- `(audit_case_key, ordinal)` composite primary key, implemented `WITHOUT ROWID` in v1;
- `invocation_id`, the 16-byte UUID value reserved durably in the inbox before the provider call; unique with `audit_case_key`;
- `attempted_at_ms`;
- `outcome_code` small integer from a versioned mapping;
- `semantic_fingerprint` nullable 32-byte BLOB;
- `receipt_sha256` nullable 32-byte BLOB;
- `diagnostic_sha256` nullable 32-byte BLOB;
- `span_ordinal` nullable integer;
- no JSON payload and no repeated error string.

`trade_lifecycle_observation_spans` — one row per contiguous admitted semantic state:

- `(audit_case_key, span_ordinal)` composite primary key, implemented `WITHOUT ROWID`;
- semantic schema and semantic fingerprint;
- `first_evidence_id`, foreign-keyed to the existing evidence primary key; an
  insert/update trigger additionally requires that evidence's `case_id` to
  equal the case mapped by `audit_case_key` (the span deliberately does not
  repeat the text case id);
- first and last successful-observation ordinals and timestamps;
- successful observation count and intervening failed-attempt count;
- current chain hash at close;
- `last_receipt_sha256` nullable;
- `closed_at_ms` nullable;
- no repeated text `case_id`; the audit head is the case mapping.

`last_receipt_sha256` has an index. A receipt is unreferenced only when no
span—open or closed—references that hash. The post-commit sweep must use this
index to test the union of all span references before deleting the just-replaced
candidate; sharing an equal last receipt across cases or spans can never make
one owner's replacement delete another owner's live receipt.
`last_receipt_sha256` is also a nullable foreign key to
`trade_lifecycle_receipt_blobs`; foreign-key enforcement is enabled on every
writer connection.

`trade_lifecycle_receipt_blobs` — content-addressed storage for a span's last successful duplicate receipt only:

- `receipt_sha256` 32-byte primary key;
- codec and codec version;
- uncompressed and compressed byte counts;
- compressed payload BLOB;
- creation time.

The first receipt is already present in `trade_lifecycle_evidence`. If the last receipt is byte-identical to the first, `last_receipt_sha256` remains null and readers reuse the admitted evidence. Otherwise, each open or closed span references at most one additional full receipt. Equal last receipts across spans reuse the same content-addressed blob.

### 4.4 Ordered audit chain

For ordinal `n`, the chain is:

```text
chain_n = SHA256(
  chain_schema || chain_(n-1) || case_id || ordinal || invocation_id || attempted_at_ms ||
  outcome_code || semantic_fingerprint_or_zero || receipt_sha256_or_zero ||
  diagnostic_sha256_or_zero
)
```

Only the head and a closed span store the resulting chain; intermediate rows store the ordered inputs, not another 32-byte chain copy. Full verification is an explicit offline O(N) operation. Hot writes are O(1) in history length. The compact integer case/span keys avoid repeating long text identifiers in every row. `invocation_id` makes a retry after an uncertain commit a no-op instead of a second ordinal.

Retained run manifests seal the relevant `(case_id, last_ordinal, chain_sha256)` heads. This detects accidental row loss or rewriting against a retained seal; it is not a claim of tamper-proof non-repudiation if an attacker can rewrite both the database and every retained manifest.

### 4.5 State machine and transaction boundary

For a valid observation, semantic projection, admission decision, evidence insert when needed, audit append, span update, last-receipt reference move, admission-head advance, and transaction commit are one SQLite transaction.  Physical deletion of any newly orphaned receipt blob is post-commit and is not part of this atomic list.

Before calling the provider, the inbox claim transaction reserves a durable RFC 4122 UUIDv4 `invocation_id` and keeps it until that attempt is completed or explicitly reconciled. The ledger stores its 16-byte UUID representation and enforces `UNIQUE(audit_case_key, invocation_id)` with idempotent `INSERT ... ON CONFLICT DO NOTHING`. Lease renewal or process restart reuses that value; a genuinely new provider call must first reserve a new invocation id. Random process-local IDs created after the call are forbidden because they cannot distinguish retry-after-commit from a second call.

- First valid observation: insert admitted evidence, open a span, append the next audit ordinal (ordinal 1 only when no failed attempt preceded it), and advance both heads.
- Same schema and fingerprint: do not insert admitted evidence; append one compact audit row; update the open span; atomically replace its last-receipt reference.
- Changed schema or fingerprint: close the old span, insert admitted evidence through the existing path, and open a new span. A semantic schema version change always opens a new span even if projected values happen to match.
- Provider failure after a real call: append a compact audit row with no span change and no receipt blob. If a span is open, increment its intervening-failure count.
- Static block before a provider call: update operational retry state only; do not claim a provider attempt.
- Stale generation discovered after the provider call: append an attempted-but-not-admitted audit outcome with no span/receipt change. A stale short circuit before the provider call adds no audit row.
- Crash before commit: none of the new evidence/audit/span state is visible. Retry uses the same admission/idempotency rules.

The ledger audit transaction is authoritative even though operational retry completion happens later in the separate inbox database. If ledger commit succeeds and inbox completion fails, retrying the same `invocation_id` does not append a second audit row; control-state reconciliation reads the audit head and advances only the overwriteable retry state.

Blob liveness is determined by actual span references and mark-and-sweep, not a mutable reference counter. The old moving “last” reference is replaced in the transaction; physical deletion of an unreferenced receipt blob happens in a bounded post-commit sweep, so a rollback can never restore a span reference to a deleted blob. That sweep is indexed by receipt hash, deletes at most the just-replaced candidate per attempt, and is idempotent. Existing admitted evidence is never garbage-collected by this mechanism.

## 5. Frozen decision B — global source generation with per-account heads; deterministic full replay only on writes

### 5.1 Normalized account key

Phase 3A adds an additive nullable `account` column to `trade_events` and `position_lots`, backfills it from canonical JSON, validates lowercase account labels, and indexes:

- `trade_events(account, trade_time_ms, event_id)`;
- `position_lots(account, expiration, record_id)`.

During a mixed-version window, triggers may derive the key from JSON only when the new column is null. A missing or conflicting account fails migration/readiness; it is never silently assigned. `NOT NULL` enforcement is a later schema rebuild after old writers are retired.

### 5.2 Generation state

`position_projection_source_state` is a singleton row:

- `source_generation`: incremented for every projection-affecting `trade_events` mutation;
- `projector_schema` and update time.

The source generation is global because a `void` or `repair` event may target another event, projection validates a cross-event graph, and the first implementation deliberately retains the existing deterministic full replay. Claiming per-account source independence before proving that graph can be partitioned would be unsafe.

The current `trade_events` table has only `event_id`, `event_json`, `trade_time_ms`, `created_at_ms`, and `updated_at_ms`. Trigger semantics are frozen as follows:

- `INSERT` and `DELETE` increment `source_generation`;
- `UPDATE OF event_json, trade_time_ms` increments it only when the corresponding old/new value differs;
- `UPDATE OF created_at_ms, updated_at_ms` does not increment it;
- the new normalized `account` column is an integrity/index projection of `event_json`, so account-only repair cannot commit unless JSON and the normalized column agree; a real account change therefore changes `event_json` and is already covered.

Any future column must be classified as projection-affecting or metadata-only in this contract and trigger tests before schema merge. A generic `AFTER UPDATE` trigger is forbidden.

`position_projection_heads` has one row per account:

- `built_source_generation`: source generation consumed by the last successful publish;
- `lots_generation`: incremented for every physical lot mutation for the account;
- `built_lots_generation`: lot generation captured by the last successful publish;
- `projection_fingerprint`: canonical hash of that account's projected lots;
- `projector_schema`, status, and update time.

SQLite triggers own the global `source_generation` and per-account `lots_generation`. Application writers cannot directly increment or reset them. The projector alone owns the `built_*`, fingerprint, schema, and trusted status fields, and advances all account heads only after all lots have been published in the same transaction.

### 5.3 Complete mutation matrix

| Mutation | Generation effect | Required publication behavior |
|---|---|---|
| New canonical `trade_events` insert | increment global `source_generation` | full deterministic replay and publish in the same writer transaction |
| Idempotent insert with identical existing event | none | no replay required |
| Conflicting same-id event | reject | no state change |
| Append-only void/correction/repair event | same as insert | replay and publish in the same transaction |
| Direct event JSON/account update used by a controlled migration | increment global `source_generation` once | migration must publish before commit or leave all heads explicitly dirty |
| Event delete used by an explicitly authorized repair | increment global `source_generation` once | repair must publish before commit or leave all heads dirty |
| Lifecycle mutation that does not append a trade event | none | no position replay |
| Lifecycle resolution that appends terminal trade events | event trigger owns the change | replay and publish in that transaction |
| Any `position_lots` insert/update/delete | increment `lots_generation` for the affected account | legitimate projector captures final value at transaction end; any later direct mutation causes mismatch |

The first implementation retains deterministic full replay computation but retires global `DELETE + INSERT` publication. In the same transaction it canonicalizes current and projected lots by `record_id`, then performs DML only for added, changed, or removed rows. It must publish heads for the union of accounts present in old lots, canonical events, and new lots, including accounts whose resulting lot set is empty. Every head receives the same final global `built_source_generation`; only accounts with physical lot changes advance `lots_generation`, and every head captures its own final lot generation and fingerprint. Public `replace_position_lots` behavior remains facade-compatible even though its internal publication becomes diff-based.

This is not a per-event delta projector: projection CPU remains O(E) at the low-frequency write boundary, while SQLite DML/WAL becomes O(changed lots) rather than O(all lots). The `history_10x` writer gate below is the exit condition. If it fails, Phase 3A cannot ship with full replay and must return to plan review for a checkpoint/delta design.

### 5.4 Trusted read protocol

`read_current_position_projection(account)` runs in one SQLite read transaction:

1. read the account head;
2. require global `source_generation == head.built_source_generation` and `lots_generation == built_lots_generation`;
3. read only indexed `position_lots` for that account;
4. canonicalize lots by `record_id` using the same projection fingerprint schema, recompute the account lot fingerprint, and compare it with the head;
5. return trusted rows or a structured `data_unavailable` reason.

This reader must not list `trade_events`, project events, read another account's lots, or write the database.

The lot fingerprint schema is `position_lots_fingerprint.v1`: keep exactly `record_id` and the complete `fields` mapping; sort lot rows by `record_id`; recursively sort object keys; preserve list order; preserve missing-versus-null; reject NaN/Infinity; and serialize with UTF-8 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`. Existing schema-valid timestamp/string/number representations are not silently coerced. Writer, reader, shadow comparator, and offline verifier must call the same domain-owned canonicalizer rather than copy this algorithm.

Lot-generation/fingerprint mismatch in scan or ordinary quality paths is fail-closed for that account only. A global source-generation mismatch means the last full projection did not publish and therefore fails closed all account heads; that blast radius is an explicit consequence of retaining a global projector, not a false account-isolation claim. Both cases emit a bounded diagnostic and exit quickly. Recovery is the existing explicit rebuild facade extended with generation publication, or a separately scheduled reconcile. There is no automatic replay inside tick, scan, snapshot read, or regular quality refresh.

### 5.5 Current decision projection

Eliminating trade replay alone is insufficient: the current `decision_state_snapshot` also lists all lifecycle evidence/allocation/source-consumption rows, all assigned-stock events, and recomputes lifecycle overlay and combo membership. Phase 3B therefore publishes one `current_decision_projection` per account in the same SQLite transaction as any owning ledger mutation.

The projection contains only the decision-bearing current facts already exposed by the existing snapshot contract:

- the trusted position-head binding and current-lot-derived overlay/combo facts;
  the lot rows themselves remain only in indexed `position_lots` and are not
  copied into this JSON;
- operational lifecycle cases (pending, conflict, needs-review, or still
  referenced by a current lot, reservation, live combo, or assigned-stock
  view), their current resolved facts, generation-token bindings, active
  reservations, and timing policies;
- current combo membership facts for live/current groups;
- current assigned-stock view required by current position context, not its event history;
- compact lifecycle quality facts per operational case, including
  admitted-evidence count, immutable status inputs, and deadline timestamp, so
  ordinary quality does not count historical evidence rows;
- generation bindings for the canonical tables from which these facts were derived;
- canonical fingerprint, projector schema, and update time.

It does not embed terminal unreferenced case history, historical evidence, trade-event, allocation, source-consumption, or assigned-stock rows. Those remain queryable through explicit audit/replay APIs. Ordinary quality changes from one detailed dataset per terminal lifecycle case to account/market aggregate terminal counts/statuses plus detailed facts only for operational cases. The former detailed terminal-case output remains available through an explicit scheduled/manual audit artifact. Therefore the hot projection is bounded by current operational state rather than lifetime case count.

That output-granularity change is a public data-contract migration, not an
internal optimization. Phase 1 must inventory every reader of
`dataset_id=om.lifecycle_evidence` and `scope.lifecycle_case_id`. Phase 3B must
dual-publish legacy detailed quality output and the proposed aggregate output,
compare aggregate counts/statuses and operational-case details, and obtain zero
unexplained legacy-consumer reads for the same 14 eligible market days used by
the artifact cutover. Until that gate passes, Phase 5 cannot retire the legacy
detailed ordinary-quality output.

Generation ownership is table-driven: existing per-case evidence revisions cover evidence invalidation; additive revision triggers cover lifecycle cases, allocations, source consumptions, timing policies, combo identities, and assigned-stock events. These triggers update compact per-account decision-input revision rows. For a case-scoped table, the trigger resolves account through `trade_lifecycle_cases`; for an update that changes ownership, both old and new accounts advance. A missing case/account mapping aborts the mutation rather than advancing an anonymous global bucket. The global position source generation and per-account lot generation are also inputs. A writer that mutates any input must either publish the affected account projection before commit or leave its generation binding mismatched. Global event-generation change republishes every account because combo/void relationships are not yet proven partitionable.

The decision-specific revision trigger set is closed in v1: it covers
`trade_lifecycle_cases`, admitted `trade_lifecycle_evidence`,
`trade_lifecycle_allocations`, `trade_lifecycle_source_consumptions`,
`trade_lifecycle_timing_policies`, `strategy_group_identities`,
and `assigned_stock_events`. `trade_events` and `position_lots` are deliberately
not given duplicate decision-specific counters; the decision projection binds
their existing global position source generation and per-account lot generation
directly. Phase 3B first adds the normalized account/owner keys and supporting
indexes needed to resolve the affected account without scanning JSON history.
The audit-only
`trade_lifecycle_attempt_audit_heads`, `trade_lifecycle_attempt_audits`,
`trade_lifecycle_observation_spans`, and `trade_lifecycle_receipt_blobs` are
explicitly excluded, as are notification/outbox/delivery and diagnostic tables.
Changing this set requires a contract and trigger-test amendment; a generic
"any ledger table" revision trigger is forbidden.

Those triggers implement invalidation only. Each affected account has one
`decision_input_generation`; dirty is derived by comparing it with the
projection's `built_decision_input_generation`, not persisted separately. A
trigger resolves the old/new account and advances that small row, but it never
decodes or rewrites the
decision JSON, replays events, recomputes overlay/combo, or publishes a head.
Multiple source mutations in one SQLite transaction may advance the source
generation more than once, but they are coalesced into the same affected-account
set and the transaction-end publisher writes at most one decision projection
row per affected account. The publisher captures the final generation and
advances `built_decision_input_generation` only after that one successful
publish. A duplicate provider
attempt and excluded-table write advance nothing. A direct SQL mutation of an
included source table may leave the account dirty; reads then fail closed until
explicit repair instead of silently trusting stale JSON.

The implementation inventory for this projection must cover at least `upsert_trade_lifecycle_case`, `insert/update trade_lifecycle_evidence`, `update_trade_lifecycle_case_derived_status`, allocation/source-consumption/timing-policy inserts, `insert_strategy_group_identity`, `upsert_assigned_stock_event`, and all trade-event/position-lot writers. Notification outbox/batch and combo-pair proposal rows are excluded unless code evidence shows they alter the snapshot facts above. Direct SQL migration/repair mutations are covered by the same triggers and leave heads dirty if they do not republish.

Publication cost is paid only on semantic/current-fact change. A duplicate settlement observation whose admission head, case resolution, lots, combo identities, and assigned-stock view are unchanged appends the compact audit sidecar but does not rebuild or rewrite `current_decision_projection`. The trigger revisions used for this projection therefore track decision-bearing changes, not every audit attempt.

When one transaction changes lots and decision facts, publication order is
frozen: finish the Phase 3A lot diff, capture the transaction's final
`built_source_generation`, `built_lots_generation`, and lot fingerprint, then
write the decision projection with exactly those final bindings, and only then
advance its built decision-input revision. Reading pre-publication lot heads is
forbidden. The transaction either exposes all final bindings together or none.

The normal publisher receives the validated old/new mutation facts already in
the owning command transaction. It updates the prior compact projection and may
recompute overlay/combo only from indexed current lots plus current operational
facts. Its work is O(current account state + changed facts), independent of
lifetime evidence/case/assigned-stock history. It must not list historical
tables to reconstruct the projection. An initial migration backfill or explicit
repair may rebuild from full history offline, publishes a separate status
artifact, and is never called by scan, tick, ordinary quality, or a normal
decision-bearing write.

`read_current_decision_projection(account)` reads the projection, indexed account
lots, and all live generation/revision heads in one transaction. It requires
the embedded global position source generation, account lot generation and
fingerprint, and every account decision-input revision to equal their live
heads, then verifies both canonical fingerprints. Integrity without exact
generation equality is never fresh. A local lifecycle/combo/assigned-stock/lot
binding mismatch returns account-scoped `data_unavailable`; a global position
source-generation mismatch returns `data_unavailable` for every account. The
reader performs no fallback, replay, repair, or write after a head exists.
Ordinary scan/snapshot/quality consumers use this reader, including ledger
projection trust and lifecycle evidence-count/deadline checks. Explicit
lifecycle reconcile/write flows may use the existing detailed rows because
they are low-frequency mutation paths and need compare-and-set evidence.

The projection stores immutable timing inputs such as
`settlement_deadline_ms`, not wall-clock classifications such as "overdue" or
"remaining duration". Readers derive those classifications from an injected
`now` without a database write. Any future time-driven stored state requires a
named scheduler owner and its own revision contract.

The decision-specific payload is stored as one canonical JSON document per
account in v1. This keeps its hot read to one indexed row and one bounded
decode/hash while reusing `position_lots` as the single physical current-lot
projection. Partial normalized tables are deferred until measured current-state
size or update cost violates the budgets; implementation must not
pre-emptively create a second normalized projection graph.

### 5.6 Conditional checkpoint-and-tail projection

Full replay remains the Phase 3A default only while the `history_10x` budget
passes. If it fails, implementation stops: it does not silently relax the
threshold or substitute an ad-hoc incremental projector. A focused planreview
must first freeze and approve the following bounded checkpoint path.

The path is **checkpoint + deterministic tail replay**, not a second ledger and
not an unrestricted per-event delta state machine:

- `trade_events` remains the only event authority; `position_lots` remains the
  only current-lot projection.
- A checkpoint binds `projector_schema`, exact ordered event-prefix count,
  prefix-end `(trade_time_ms, event_id)`, prefix hash/chain head, full source
  generation, canonical accumulator payload/hash, creation time, and verification
  status. The accumulator contains only state required to resume the current
  projector exactly: current lots, open-event facts needed for later fee
  allocation, allocated-open-fee state, allocation-sequence state, current
  strategy/adjust projection inputs, and a bounded publishability/diagnostic
  summary. It must not discard information that changes current lots, risk
  views, public `position_lots` records, or the publishability verdict.
- The fast path is an internal **current-position publication** entrypoint. It
  does not serve APIs that request the complete historical
  `ProjectionResult.allocations` or every per-event diagnostic; those remain
  explicit offline/full-replay reads. Prefix allocation/diagnostic history is
  represented in the checkpoint only by the resumable current accumulator,
  severity/count/hash summary, and bounded samples—not by copying O(E) rows.
  Phase 1 inventory must prove that no synchronous writer facade requires the
  complete prefix lists; any such facade remains on full replay.
- Keep the newest two verified checkpoints plus one last-known-good checkpoint.
  Creating a new one and retiring an older cache are atomic within SQLite;
  checkpoints are rebuildable caches and their fixed `K <= 3` bound makes space
  O(K × resumable current state), not O(event history).
- A normal append may resume from the newest verified checkpoint and replay the
  strict ordered suffix only when every suffix event is proven append-safe.
  Append-safe means no event reorders before the prefix end, no `void`/`repair`
  targets an event inside or before the checkpoint prefix, no controlled
  UPDATE/DELETE touches that prefix, and the projector schema/prefix hash match.
- Hot-path append safety is derived from the current mutation, indexed target
  lookup, checkpoint boundary, and its trusted/invalidated flag; the writer does
  not rescan/re-hash the full prefix on every append. Insert/update/delete
  triggers conservatively invalidate intersecting checkpoints. A direct or
  unclassified mutation invalidates all checkpoints rather than trying to
  infer safety from JSON.
- A `void`, repair pair, late/backdated event, controlled UPDATE/DELETE, schema
  change, prefix-hash mismatch, or any unproven dependency invalidates every
  checkpoint whose prefix intersects the dependency. The writer chooses the
  newest earlier verified checkpoint that is provably unaffected; if none
  exists it performs the canonical full replay. Correctness fallback is always
  allowed; tail replay is never used on an unproven graph.
- Checkpoint/tail output must have exactly the same current lots, risk views,
  public position records, publishability verdict, and canonical fingerprints
  as a full replay; its diagnostic count/hash summary must also match the full
  oracle. One shared domain projector owns both entry modes; application code
  may not implement parallel business semantics.
- After activation, an explicit scheduled/manual verifier periodically performs
  a full replay and compares checkpoint/tail output. Any mismatch marks the
  checkpoint path untrusted, fails closed for publication, emits a status
  artifact, and returns writers to full replay until repair.

Activation requires measurable benefit as well as correctness: full replay
must first fail the frozen gate; checkpoint/tail must then pass the budgets in
section 10, reduce reference-host p95 wall/CPU by at least 50%, and stay within
the fixed checkpoint storage bound. Otherwise the simpler full replay remains
the approved implementation.

## 6. Frozen decision C — runtime snapshot integrity without false temporal claims

`runtime_portfolio_snapshot.v1` is a run-bound envelope with independent sections:

- `ledger_projection`;
- `broker_cash`;
- `broker_positions`;
- derived cash-occupation facts;
- source status and completeness.

Every source section records account, schema, source-observed time, application-received time, receipt/content hash, completeness, and the source's existing freshness result. The top-level seal covers canonical content and run/account binding only.

The envelope records descriptive minimum/maximum observed times and measured skew. Version 1 does not introduce a new cross-source skew gate and does not claim simultaneity. Consumers continue to fail closed using each source's existing freshness/completeness contract. A future business rule that blocks on cross-source skew requires a separately reviewed numeric policy; it is not smuggled into this storage change.

The compact runtime snapshot contains current position lots, current cash/headroom facts, and the chosen run inputs/results. It does not contain full trade history, full lifecycle evidence history, embedded scan raw JSON, or base64 copies.

## 7. Frozen decision D — scan blobs, roots, retention, and CSV/base64 cutover

### 7.1 Filesystem blob protocol

Large immutable scan payloads use a filesystem content store separate from the SQLite ledger:

```text
output_shared/blobs/sha256/<first-two-hex>/<sha256>.json.gz
```

- Hash the uncompressed canonical bytes.
- Use deterministic gzip with a fixed codec version and `mtime=0`.
- Write a temporary file in the target directory, fsync as supported, then atomically rename.
- Concurrent publication of the same hash is idempotent.
- Publish the run/artifact manifest only after the blob is durable.
- Readers decompress and recompute the uncompressed hash.

Run manifests hold hashes, schemas, byte counts, and logical roles; they never inline the same payload as base64 once cutover completes.

### 7.2 Root manifests and mark-and-sweep

The protected root set is the union of:

- every retained online run manifest;
- research archive manifests;
- Shadow Replay dataset, experiment, mark, and settled-outcome manifests;
- permanent daily representative manifests;
- explicit manual keep/pin manifests.

Retention of ordinary runs preserves the current union rule: keep a run if it is within 14 days **or** among the latest 200. A run is eligible for deletion only when neither condition holds. A blob is eligible only when no protected root reaches it and its publish time is older than a 24-hour orphan grace period.

GC is mark-and-sweep with a read-only preview, deterministic plan hash, reference-integrity validation, and explicit confirmation. It does not use file mtime alone and does not use mutable reference counts. Blob-before-manifest crashes create harmless orphans that are recoverable until grace expiry. Missing/corrupt referenced blobs block deletion and mark the owning artifact incomplete.

### 7.3 Frozen consumer inventory and cutover gate

The minimum known consumer inventory is:

| Contract | Current dependency | Required migration proof |
|---|---|---|
| required-data producer | raw JSON + CSV and inline base64 bundle in `opend_symbol_outputs.py` | canonical blob manifest emitted and validated |
| receipt verifier/reentry | decodes and byte-compares both base64 fields | blob-aware verifier with identical integrity result |
| Shadow Replay marking | globs `*_required_data.csv` | canonical reader produces identical marks |
| scan/filter/coverage paths | candidate, CC/LP, Sell Put/Call helper, prefetch, multiplier, quote-cache, and coverage modules read `*_required_data.csv` | shared canonical frame reader returns decision-identical rows |
| close advice / Daily Brief | frozen required-data snapshot resolves CSV bytes and pandas frames | canonical materializer is byte/field equivalent for decision inputs |
| materialization and research CLI | tools accept a required-data directory/CSV contract | facade accepts canonical manifest and legacy directory during the window |
| Strategy Lab dataset/outcome build | run paths and candidate/mark artifacts | end-to-end build/mark/settle with blob references only |
| research archive | copies or inventories run paths | archive manifest retains reachable blob hashes |
| diagnostics/tests | direct CSV/base64 fixtures | contract tests cover canonical and legacy adapter paths |

Cutover order is fixed:

1. complete the full `rg`/runtime inventory and add payload-free legacy-read counters;
2. make every reader prefer the canonical manifest/blob and fall back to legacy data;
3. shadow-compare canonical versus legacy decoded values;
4. run Strategy Lab build/mark/settle and research archive end to end;
5. require zero unexplained legacy reads for 14 consecutive eligible market days in both configured markets; an eligible day is a scheduled market-open day on which the configured account/strategy was enabled, and upstream failure remains in the denominator as incomplete rather than being discarded;
6. stop default CSV/base64 persistence behind an explicit gate;
7. retain a one-version on-demand compatibility materializer that reconstructs a legacy view from the canonical blob without storing a second default copy.

Stopping legacy output before all seven gates pass is a contract violation.

### 7.4 Research storage classes, dataset generations, and capacity control

Logical retention and physical placement are separate decisions. A permanent
research/replay root means its exact content remains addressable and verifiable;
it does not require every derived representation or every physical copy to stay
in each run directory.

Every research artifact is classified before Phase 6/7 cutover:

| Class | Examples | Retention and physical strategy |
|---|---|---|
| immutable replay authority | sealed run input/output, candidate and rejection facts, source status, marks, settled outcomes, trade/outcome references | permanent logical root; manifest and hashes retained; large payloads content-addressed and deduplicated |
| experiment/provenance metadata | dataset generation, parameters, scorecard, proposal, holdout identity, software/schema versions | permanently retained compact manifest/JSON; never copied with the full dataset |
| immutable shared partition | canonical required-data/research rows grouped by schema/market/date/account scope | stored once by content hash; many dataset generations reference the same partition |
| reproducible compatibility/derived cache | CSV, inline base64, temporary decoded bytes/DataFrame, materialized legacy directory, report rendering | not a permanent root after its migration gate; generated on demand from canonical blobs; existing copies are not deleted without authorization |

Research datasets use immutable **base + partition generations**. A generation
manifest binds its parent generation (if any), ordered added/removed partition
hashes, schema/provenance, logical row/count/hash summary, and a fully resolved
generation hash. An experiment binds that immutable generation id. Creating a
new generation writes only new unique partitions plus compact manifest metadata;
it must not copy every prior partition into a new dataset directory. Reading a
generation resolves its complete partition set deterministically and verifies
every hash. Compaction may publish a new equivalent base manifest, but old
generation ids and reachability remain valid. Parent-delta depth is capped at
32. Creating generation 33 publishes a new base manifest containing the fully
resolved ordered partition-hash list (metadata only, no payload copy), then
starts a new delta chain. Resolution is therefore O(partition hashes + at most
32 delta manifests), never O(all historical generations).

Physical placement has three explicit tiers:

1. **hot** — retained online runs and recent active research generations on the
   local runtime filesystem;
2. **warm** — older local canonical gzip blobs still reachable by permanent
   manifests, with no duplicate CSV/base64/run-directory copy;
3. **cold candidate** — old, unique, large blobs selected by a read-only plan
   for a future content-addressed archive backend. No backend or local eviction
   is activated in this contract.

A future cold tier must preserve the same hash address, encryption/access
boundary, manifest reachability, and restore verification. Activation requires
an explicit reviewed backend, cost and restore-SLA contract. Copy-and-verify and
evict-local are two separate operations; local eviction or deletion still needs
explicit user authorization. Strategy Lab may restore through the canonical
reader, but a missing/unverified cold object is `data_unavailable`, never a
partial silent replay.

The storage status artifact reports at least: logical referenced bytes, unique
physical bytes, deduplication ratio, bytes by class/tier/market, orphan bytes,
new unique bytes per month, trailing-three-month growth, 90-day forecast, root
and generation counts, largest new unique partitions, and restore/integrity
status. It is read-only and payload-free. It raises:

- **warning** when forecast free space after 90 days is below
  `max(10% of filesystem capacity, 20 GiB)`, or monthly new-unique-byte growth
  exceeds twice the trailing-three-month median for two consecutive months;
- **critical** when current free space is below
  `max(5% of filesystem capacity, 10 GiB)`, a protected object is missing or
  corrupt, or a generation cannot be resolved.

Warnings and critical status create an operator decision point and a
deterministic tiering/cleanup preview; they never automatically remove a root,
move data off-host, delete a blob, or change Strategy Lab eligibility.

For the Phase 1 payload-free collector, `integrity` is split into manifest
parseability, declared-reference presence/size, and content verification.
Manifest-declared hashes may be used for logical dedup accounting, but content
is `not_verified` unless a separately executed verifier receipt is present;
the collector never refreshes that receipt or claims that current bytes match
it. Unmanifested bytes remain unknown-unique. Monthly growth and the 90-day
forecast are derived only from two or more compatible timestamped baseline
reports for the same resolved runtime root, never from file mtime alone. Missing
history produces `insufficient_history`, not zero growth.

## 8. Strategy Lab and replay contract

The optimization must not remove either class of research input:

- exact sealed run input/output: account positions, cash/headroom, candidate facts, selected/rejected results, source status, and blob hashes;
- canonical trade events and closed outcome facts required to evaluate a strategy.

The compact snapshot is sufficient to replay what the scanner saw and produced at that run. It intentionally does not explain why a user held three versus five contracts before the run; that history remains in `trade_events` and position projection. Strategy Lab may join sealed run facts, candidate/mark/outcome manifests, and ledger-derived closed outcomes offline. It remains advisory and cannot write operational authorities.

Old run directories and lifecycle rows remain readable during the migration. New manifests must not depend on a mutable `latest` pointer for historical replay.

Historical replay resolves the bound research generation and content hashes,
not a physical hot/warm/cold path. Consequently deduplication, partition reuse,
or an explicitly approved cold restore does not change experiment inputs.
Reconstructible CSV/base64/report views are adapters, not independent evidence.

## 9. Time and space complexity contract

Let:

- `E` = total trade events;
- `L_a` = current lots for one account;
- `N_c` = provider-attempt audit rows for one lifecycle case;
- `S_c` = semantic spans for one lifecycle case;
- `R` = canonical receipt bytes;
- `B` = new immutable scan payload bytes;
- `U` = unique content bytes across retained roots.
- `K` = retained verified position checkpoints, fixed at at most 3 after activation;
- `T` = trade-event suffix after the chosen verified checkpoint;
- `P_g` = new unique research partitions added by one dataset generation.

| Operation | Required complexity | Explicitly forbidden work |
|---|---|---|
| duplicate lifecycle attempt | O(R) hash/compress plus O(1) indexed SQLite rows; independent of `N_c` | listing/revalidating lifecycle history |
| audit-chain verification | O(`N_c`) offline only | running in tick or ordinary quality refresh |
| current position read | O(`L_a`) rows and hash | reading/projecting `E`; reading other accounts |
| current decision read | O(`L_a` + current compact decision bytes for account) | listing lifecycle/assigned-stock history or recomputing overlay/combo |
| trade writer | O(`E`) full replay is temporarily accepted because writes are low-frequency | a second replay after commit |
| checkpoint trade writer, only after activation | O(`T` + changed lots) with bounded checkpoint decode; worst-case correctness fallback O(`E`) | using a checkpoint after an unproven back-reference/reorder/prefix mutation |
| normal current-decision publish | O(current account state + changed facts) | listing lifetime lifecycle/evidence/assigned-stock history |
| ordinary quality | O(accounts + compact current decision state) | full trade replay/history listing or overlay/combo recomputation |
| runtime snapshot | O(current account state + chosen run facts) | embedding full histories or base64 payloads |
| blob publish | O(B) only for a new hash; O(1) lookup for an existing hash | rewriting one copy per account/run |
| blob GC | O(manifests + references + blobs), scheduled/manual | GC on every tick |
| new research generation | O(new manifest + `P_g` unique bytes) | copying all parent-generation payloads |

Steady-state lifecycle space is:

```text
existing admitted evidence
+ O(number of provider attempts * compact digest row)
+ O(number of semantic spans * one optional final receipt)
```

It must not be O(number of provider attempts * full receipt). Scan artifact space is O(U + manifest metadata), not O(runs * repeated payload bytes).

If checkpoint projection is activated, its additional steady-state space is
O(K × resumable current state) with `K <= 3`. Research space is O(unique
partition/blob bytes + generation/root manifests), not O(generations × full
dataset bytes). Permanent logical roots may still cause unique physical bytes
to grow; section 7.4 therefore makes growth and capacity explicit rather than
claiming a finite bound that the evidence contract cannot provide.

Canonical JSON replaces persisted duplicate formats; it is not a mandate to materialize a second in-memory copy everywhere. Large-payload writers/readers must stream file hash/compression/decompression where practical. Compatibility CSV bytes or a pandas frame may be materialized on demand for one consumer, but raw JSON bytes, base64 text, decoded bytes, CSV bytes, and a DataFrame must not all be retained concurrently beyond the narrow adapter call. Shadow comparison hashes each stream incrementally, retains only bounded mismatch samples, and never keeps canonical and legacy full payload graphs resident together. Peak-allocation and transition-footprint gates below include this adapter behavior.

## 10. Reproducible performance measurement contract

### 10.1 Harness and fixtures

Implementation adds one repository-local benchmark harness with no runtime dependency. Its checked-in manifest records schema version, Python version, SQLite version, platform, fixture seed, row counts, uncompressed payload-size distribution, compression-ratio distribution, entropy/compressibility class, Git SHA, and whether the run is process-cold or warm. Space budgets use uncompressed bytes and actual `db + wal + shm` bytes as their primary measures; compressed-byte results are reported separately and cannot make a highly compressible synthetic fixture appear representative.

The deterministic `growth_10x` suite has three orthogonal axes so history
independence cannot hide poor scaling in legitimate current state or account
fanout:

- `current_scale`: dimensions come from a read-only metadata baseline (counts,
  uncompressed byte sizes, compression ratios, and categorical field/value
  distributions only; no production payload copied). It contains deterministic
  low-, median-, and high-entropy payload classes matching the observed
  distribution rather than one repeated filler string.
- `history_10x`: ten times lifetime cardinality and uncompressed byte volume,
  with floors of 100,000 lifecycle attempts and 10,000 trade events. The trade
  fixture has two reported subcases: `fixed_output` uses valid read-only events
  so projected lot/view/allocation counts stay fixed and isolates event-history
  cost; `retained_closed_lots` uses deterministic open/close pairs and exposes
  the current canonical behavior in which closed lots and allocations remain
  projected. Both preserve the same deterministic payload compressibility mix,
  and their results are never collapsed into one unexplained number.
- `current_state_10x`: ten times current lots, operational lifecycle cases,
  live combo groups, and current assigned-stock facts while lifetime/current
  ratios remain fixed.
- `account_fanout`: `max(10, 5 × baseline account count)` accounts with fixed
  per-account current state; it isolates the global-generation publication
  cost.

Each timing scenario uses 5 warm-ups and 30 measured repetitions. Process-cold means a fresh process and fresh temporary DB copy; it does not claim that the operating-system page cache was flushed. Report median and p95 wall time, CPU time, Python peak allocation, peak RSS when the platform exposes it, SQL statement/row counts, and disk bytes. Absolute timing gates run only on the recorded reference host/profile; CI on other hardware enforces correctness, call counts, query plans, allocation/space bounds, and reports timing without comparing incomparable hosts.

Timing runs do not enable `cProfile` or `tracemalloc`. Separate diagnostic runs collect top cumulative CPU functions and top allocation sites, so instrumentation overhead cannot make a threshold pass or fail. `time.perf_counter_ns()` and `time.process_time_ns()` are the portable timing authorities; no benchmark plugin is required.

SQLite steady-state size is measured on a temporary fixture after `wal_checkpoint(TRUNCATE)`; a copied temporary DB may be vacuumed only for normalized comparison. Peak `db + wal + shm` bytes are also reported separately so moving-last-receipt churn cannot hide in WAL.

The moving-last-receipt benchmark runs at 64 KiB and at the read-only baseline p99 receipt size. Its persistence stopwatch begins before canonical hash/compression and ends after the bounded post-commit orphan sweep. Peak WAL growth per duplicate observation after steady-state auto-checkpointing must be ≤ `max(1 MiB, 4 × R)`; across 1,000 observations, peak `db + wal + shm` growth above retained compact rows and the one live receipt must be ≤ `max(64 MiB, 8 × R)`, and it must return to the steady-state envelope after `wal_checkpoint(TRUNCATE)`. If the existing SQLite auto-checkpoint policy cannot meet that bound, Phase 2 must add a reviewed connection-level checkpoint/journal-size policy; it may not checkpoint on every attempt.

### 10.2 Pass/fail budgets

| Scenario | Required budget |
|---|---|
| duplicate observation with 100,000 prior audit rows, 64 KiB canonical receipt | p95 persistence wall ≤ 25 ms; incremental compact DB ≤ 224 bytes per attempt averaged over 100,000 rows; Python peak allocation ≤ max(8 MiB, 3 × R); only one optional last-receipt blob remains live for the span; repeated `invocation_id` adds zero bytes/rows; WAL obeys the envelope above |
| position generation tracking overhead on a normal trade write | p95 added wall/CPU ≤ max(10 ms, 10% of the baseline writer); no extra full replay |
| deterministic full replay and diff publication, `history_10x` (10,000 events floor) | p95 wall ≤ 2 s and p95 CPU ≤ 1.5 s on the recorded reference host; SQLite changed-row count ≤ added + changed + removed lots plus head rows; no global lot-table delete/reinsert; failure blocks Phase 3A and requires a new checkpoint/delta planreview |
| conditional checkpoint/tail append after focused approval | current lots/views/public records/publishability and canonical fingerprints are equivalent to full replay, diagnostic count/hash summary matches, and complete historical allocation/diagnostic APIs stay on full replay; p95 wall and CPU improve by at least 50% versus failing full replay and are each ≤ 500 ms when tail ≤ 1% of event history; peak allocation ≤ max(64 MiB, 2 × checkpoint bytes); retained checkpoints ≤ 3 and checkpoint bytes ≤ 3 × resumable-current-state bytes plus 10% metadata; no-prefix-mutation fixture reads only checkpoint + tail and performs zero full-prefix hash/scan calls |
| conditional checkpoint invalidation (`void`/repair/backdate/update/delete/schema/prefix mismatch) | correct earlier-checkpoint or full-replay output is exactly equivalent; zero unsafe-tail publications; invalidation lookup p95 ≤ 50 ms before replay; mismatch marks checkpoint path untrusted |
| current-decision publication after a decision-bearing write with `history_10x` and fixed current state | p95 added wall/CPU ≤ max(25 ms, 15% of the baseline writer); one account JSON row rewritten for account-local changes, all rows only for a global event-generation change; duplicate observations cause zero projection writes; lifetime history reader call counts are zero |
| current-decision publication after global generation change, `account_fanout` | p95 wall ≤ 250 ms and CPU ≤ 200 ms; peak allocation ≤ 64 MiB; projection/head writes ≤ account count plus changed lot/head rows; lifetime-history reader calls are zero; failure requires a per-account partition/checkpoint planreview |
| trusted current-position read, current scale | p95 wall ≤ 50 ms and Python peak allocation ≤ 16 MiB per account |
| trusted current-position read, `current_state_10x` | p95 wall ≤ 200 ms; SQL plan uses the account index; trade-event reader call count is zero |
| generation mismatch read | p95 wall ≤ 50 ms; zero writes and zero replay calls; lot mismatch blocks only its account, while global source mismatch explicitly blocks all heads |
| trusted current-decision read, `history_10x` with fixed current state | p95 wall ≤ 200 ms and peak allocation ≤ 32 MiB; event/evidence/allocation/source-consumption/assigned-stock history readers and overlay/combo recomputation call counts are zero |
| trusted current-decision read, `current_state_10x` | p95 wall ≤ 500 ms; decision JSON < 1 MiB per account; peak allocation ≤ 64 MiB; failure returns Phase 3B to planreview for a measured normalized projection rather than silently raising the bound |
| ordinary control-plane quality, no external refresh | p95 wall < 10 s and CPU ≤ 25% of the current baseline; event/evidence/allocation/source-consumption/assigned-stock history reader and overlay/combo recomputation call counts are zero |
| compact runtime snapshot | uncompressed canonical JSON < 1 MiB per account; p95 build ≤ 250 ms after source sections are supplied; Python peak allocation ≤ 32 MiB |
| ordinary run directory after blob cutover | < 30 MiB of per-run physical bytes, excluding shared blob targets; no inline base64 copy |
| canonical required-data blob write/read at baseline p99 payload | p95 wall no slower than legacy by >10%; Python peak allocation ≤ max(32 MiB, 2 × uncompressed payload); persisted run+shared bytes no greater than one canonical compressed payload plus manifests |
| dual-output/shadow-compare transition at baseline p99 payload | peak Python allocation ≤ max(48 MiB, 2.5 × uncompressed payload); peak temporary + retained transition bytes ≤ two encoded representations plus manifests; comparator uses streaming hashes and retains at most 10 bounded mismatch samples |
| research generation append with unchanged parent plus one new partition | physical growth ≤ new unique compressed partition bytes + 64 KiB manifest/index overhead; parent partitions are hard/hash references, not copies; generation resolution reproduces the full logical hash |
| research generation resolution, 10,000 partition hashes and maximum delta depth | p95 wall ≤ 2 s; peak allocation ≤ 64 MiB; reads at most 32 delta manifests plus one base; zero partition payload reads until a consumer requests them |
| research storage status | p95 wall ≤ 5 s on `history_10x`; peak allocation ≤ 64 MiB; reads metadata/manifests/filesystem sizes only, never payload contents; forecasts and thresholds are deterministic |

Absolute thresholds and “no forbidden call” checks are both mandatory. Faster hardware cannot excuse an O(history) hot path. Any threshold adjustment requires a new baseline artifact and plan amendment, not a silent test change.

## 11. Phase gates and implementation order

### Phase 1 — contract, inventory, and baseline

- Freeze this document through planreview.
- Record read-only metadata baselines and deterministic synthetic fixture manifests.
- Complete writer/reader/CSV/base64/root-manifest inventories.
- Produce benchmark baseline JSON and profiles.
- Produce a read-only research-storage inventory and capacity status artifact:
  logical versus unique physical bytes, dedup ratio, growth history, 90-day
  forecast, and candidate hot/warm/cold classification. No file moves/deletes.
- No schema or production data change.

### Phase 2 — lifecycle stop-growth and audit proof

- Extend existing semantic admission with the compact sidecar transaction.
- Keep admitted evidence and all existing foreign keys unchanged.
- Shadow-verify counts, ordinals, chains, first/last receipts, and crash recovery.
- Do not compact history.

### Phase 3A — current position generation

- Add normalized account columns and per-account heads additively.
- Cover every mutation row in the matrix with transaction and direct-mutation tests.
- Replace global lot-table delete/reinsert with diff publication.
- Keep write-time deterministic full replay only if the `history_10x` writer gate passes; cut the current-position reader only after shadow comparison.
- If that gate fails, stop Phase 3A and run a focused planreview of section 5.6;
  checkpoint/tail projection is not implicitly authorized by this amendment.
- Scan mismatch remains read-only and fail-closed.

### Phase 3B — current decision projection

- Add one per-account source-generation row (dirty is derived from source vs
  built generation), narrow invalidation-only triggers,
  and the per-account current decision projection.
- Coalesce all affected accounts and publish at most once per account at the
  transaction boundary; triggers never build projections.
- Shadow-compare every decision-bearing fact with the legacy snapshot.
- Dual-publish and inventory the lifecycle-quality granularity change; do not retire detailed ordinary-quality output yet.
- Complete 3B independently of the Phase 3A current-position reader cutover; Phase 4 consumes both only after both heads are trusted.

### Phase 4 — compact runtime snapshot

- Emit independent source sections and integrity-only top-level seal.
- Dual-output with the current snapshot and compare every decision-bearing field.

### Phase 5 — quality hot-path cutover

- Ordinary quality reads heads/current rows only.
- Full replay moves to an explicit scheduled/manual integrity job with its own status artifact.

### Phase 6 — shared scan blob and consumer gate

- Implement blob protocol, root manifests, dry-run GC, dual readers, telemetry, and end-to-end replay tests.
- Stop default CSV/base64 only after the seven-step gate in section 7.3.

### Phase 7 — research generation and storage-tier optimization

- Optimize research state only after the runtime authorities are stable.
- Publish immutable base + partition generations and reuse content hashes; do
  not copy parent datasets and do not join research writes to ledger transactions.
- Add the read-only capacity/status surface and deterministic cold-candidate
  preview. A cold backend, data movement, local eviction, or deletion remains a
  separately reviewed and explicitly authorized follow-up.

### Phase 8 — historical cleanup preview only

- Require at least 14 eligible market days of stable new-path evidence, per-case/account reconciliation, backup proof, and a space forecast.
- Generate a deterministic deletion/compaction preview.
- Actual deletion requires a new explicit user authorization.

## 12. Migration, mixed-version, and rollback invariants

- All schema changes before Phase 8 are additive.
- Old binaries may ignore sidecar/head tables without invalidating admitted evidence or trade history.
- New readers fall back to the legacy full path only when the new schema/head is absent during the declared migration window; once a head exists but mismatches, they fail closed and never disguise corruption with fallback replay.
- A feature can be rolled back to legacy reads without removing new tables or rewriting old rows.
- Normalized account backfill is previewed and reconciled before writer cutover.
- A failed writer transaction rolls back event, lot, evidence, audit, span, and generation changes within its SQLite boundary.
- Operational retry state in the separate inbox database is allowed to lag the ledger audit after a crash; it is reconciled from the ledger head and never overwrites audit history.
- Filesystem blob publication and manifest publication are not presented as one transaction; blob-first ordering plus grace-period mark-and-sweep is the recovery contract.
- Checkpoints and decision projections are rebuildable caches. A missing,
  mismatched, or unverified cache never replaces event/evidence authority and
  causes fail-closed or canonical full-replay recovery according to its gate.
- Research generation ids and content hashes remain stable across physical
  tiering; a path move alone cannot change an experiment's logical input.

## 13. Required test matrix

At minimum, implementation tests must cover:

- identical observation repeated N times: one admitted evidence row, N compact audits, one span, bounded full receipts;
- byte-different but semantic-identical observations: same as above, with last receipt moving atomically;
- A → B → A semantic sequence: three spans and three admitted evidence states;
- provider failure between equal observations: audit ordinal continuity, no false evidence state, explicit gap count;
- ledger commit followed by inbox completion failure: same `invocation_id` is idempotent and control state reconciles without a duplicate ordinal;
- expired lease/process restart before reconciliation: no new provider call occurs until the prior durable invocation id is classified as committed or safely retryable;
- semantic schema upgrade: forced new span;
- crash injection before/after evidence, audit, blob ref, head, lots, and manifest publication;
- idempotent event, append correction, controlled update, delete repair, account move, zero-lot account, and direct lot mutation;
- two accounts with one lot-head mismatch: only that account fails closed; a global source mismatch explicitly blocks both;
- decision binding freshness: one account-local lifecycle/combo/assigned-stock revision mismatch blocks only that account; an unpublished global event generation blocks every account; neither path falls back or writes;
- same-transaction lot/decision publication captures the final lot head, never the pre-diff generation/fingerprint;
- multiple decision-bearing mutations in one transaction mark affected
  accounts but publish exactly one decision row per account; trigger bodies
  perform no projection/replay work;
- deadline classification changes across an injected `now` boundary without a database mutation;
- hot-reader spies that fail if full event/evidence readers or projector are called;
- fixed current facts over 10× lifecycle/assigned-stock history: compact decision read time, rows, and allocations remain bounded and no overlay/combo recomputation occurs;
- canonical/legacy receipt equivalence and streaming-hash Shadow Replay build/mark/settle equivalence without simultaneous full payload graphs;
- lifecycle-quality aggregate/detail dual-output equivalence and legacy-consumer telemetry;
- retained-old-run, latest-200, research-root, orphan, corrupt-blob, and concurrent-same-hash GC cases;
- two spans sharing one last-receipt hash: replacing one keeps the blob; replacing the final reference permits bounded cleanup;
- duplicate lifecycle attempt leaves every decision-input revision and projection row unchanged;
- checkpoint append-safe tail is identical to full replay for current lots,
  risk views, public records, publishability, fingerprints, and diagnostic
  count/hash summary; complete allocation/diagnostic-history APIs remain full
  replay and checkpoint storage stays bounded;
- `void`/repair targeting before a checkpoint, backdated append, controlled
  prefix update/delete, schema change, and prefix corruption choose an earlier
  safe checkpoint or full replay and never unsafe tail replay;
- checkpoint verifier mismatch disables the checkpoint path and preserves
  canonical full-replay recovery;
- research generation append reuses all unchanged parent partition hashes and
  remains exactly replayable after path-independent restore;
- storage warning/critical thresholds produce only status/preview and perform
  zero file movement, eviction, root removal, or deletion;
- benchmark threshold and complexity-call-count assertions on `current_scale`
  and all three `growth_10x` axes.

## 14. Explicitly settled review questions

1. Settlement semantic fields remain owned by the versioned semantic projector already used by admission. Any schema version change opens a new span.
2. Position source generation is global because the projector/event graph is global; lot generations and trusted heads are per account and backed by normalized account columns. Lifecycle revisions remain per case.
3. Scan mismatch never repairs. Recovery is explicit or separately scheduled and remains outside the tick budget.
4. Every actual provider attempt gets one compact ordered audit row. This is the minimum needed for verifiable continuity; only storing a count is rejected.
5. Runtime snapshot v1 introduces no cross-source simultaneity gate. It records skew and applies existing per-source freshness only.
6. Blob liveness comes from retained run, research, replay/outcome, daily, and manual-pin manifests using mark-and-sweep.
7. Legacy reads are measured by payload-free counters and must be zero for 14 eligible market days before old output stops. Scheduled market-open days remain in the denominator when upstream data is missing.
8. Full replay remains the default until its measured gate fails; checkpoint
   activation then requires a focused planreview and exact full-replay parity.
9. Permanent research evidence is logically retained through immutable
   generation/root manifests and content hashes. Physical tiering is
   path-independent, and alerts never authorize movement or deletion.

No unresolved decision in this document blocks Phase 2 or Phase 3B
implementation. Phase 3A may implement its full-replay/diff path when the
existing gate passes; if it fails, checkpoint activation is deliberately
blocked on focused planreview. Later business choices—such as a future
cross-source skew threshold, a concrete cold-storage backend, or actual local
eviction/deletion—require their own reviewed contract and authorization when
they enter scope.
