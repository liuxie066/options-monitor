# Trade Intake Settlement Collector Resilience — Minimal Correct Implementation Plan

## Decision

Implement a narrow resilience contract for the lifecycle settlement-observation collector. It covers the failure class in which a provider capability is unavailable, transiently failing, or repeatedly returns the same no-progress result while a periodic due loop keeps doing expensive work.

This is generic inside the settlement collector. Current `main` has removed the unsupported cash-flow query from the production collector; that capability remains only as a historical incident fixture in contract tests. No production requirement or scheduler branch names `cash_flow_query` or any other Futu method. This is not a general scheduler framework and does not claim to solve unrelated high-CPU loops.

The minimum correct design has two independent owners:

- source-local control state decides whether a provider attempt is currently eligible and limits calls with a lease and backoff;
- the canonical ledger writer decides, in one SQLite transaction, whether an observed semantic state is new and whether it may change evidence, lifecycle state, revision, or notification outbox.

The provider gate applies only to the `settlement_observation` branch. Local deadline reconciliation remains active even when the collector is disabled or blocked.

## Problem and root-cause contract

The current 60-second trade-intake loop invokes account due reconciliation under the shared process lock. A pairing-present case may then build an account-wide lifecycle read model, call every required settlement source, and write an observation whose identity includes attempt timestamps and diagnostic receipts. A stable capability failure can therefore become a new evidence row on every tick. The growing evidence set makes the next account read progressively more expensive, producing the observed CPU loop.

The fix must break all three links:

1. a blocked or backed-off provider branch must not enter the expensive account-evidence path;
2. runtime failure classification must be typed and must never infer permanent unsupported state from repeated unknown errors;
3. repeated semantic observations must be no-ops at the canonical writer, including after a crash, restart, or overlapping worker.

## Goals

- Keep local-only due lifecycle work running when settlement observation is disabled, blocked, or in backoff.
- Make static capability detection generic over a collector-declared requirement set.
- Bound provider calls for explicit account blocks, transient errors, unknown errors, and unchanged observed results.
- Preserve the first admitted observation timestamp while excluding attempt-only metadata from semantic equality.
- Make both issue evidence and terminal settlement evidence atomically idempotent.
- Avoid parsing account-wide historical evidence on blocked ticks.
- Preserve existing lifecycle generation compare-and-set, state derivation, allocation, revision, and notification-outbox authority.
- Expose enough bounded status to prove why a case is not being polled and when it is eligible again.

## Non-goals

- No generic job platform, retry DSL, new daemon, service split, or scheduler rewrite.
- No behavior change for non-settlement collectors or unrelated periodic CPU faults.
- No historical evidence deletion, compaction, rewrite, or revision repair.
- No automatic account-permission inference from exception text or failure count.
- No provider probe during capability preflight.
- No release, deployment, production configuration edit, service start, notification probe, or business-data repair in this work unit.

## Invariants

1. `settlement_observation` capability never gates the whole account due owner.
2. A local-only case can progress without constructing or invoking a provider collector.
3. Runtime control state is disposable. Losing it may cause at most one extra eligible provider attempt after the store is recreated, never a duplicate or lost canonical business write.
4. If the control table cannot provide an atomic claim while the inbox database itself remains usable, provider collection fails closed with `control_store_unavailable`; local-only work continues. Whole-inbox corruption remains an existing service-level fail-closed condition.
5. Only a deterministic missing API surface or an explicit allowlisted provider account-capability result may enter a blocked state. Unknown errors remain retryable with a finite cadence.
6. Attempt wall time, raw error text, request IDs, latency, and retry counters never decide semantic equality.
7. Query scope, coverage, freshness, normalized result class, rows, and settlement facts always decide semantic equality.
8. The lifecycle generation token is an optimistic concurrency check, not part of the semantic fingerprint. Adding evidence changes the generation token, so using it as semantic identity would defeat retry deduplication.
9. Semantic admission and the resulting evidence/state/revision/outbox mutation occur in one `BEGIN IMMEDIATE` ledger transaction.
10. A duplicate semantic observation reuses the stored first-seen evidence and cannot overwrite its audit timestamps.
11. A transition `A -> B -> A` is meaningful and may be recorded again. Deduplication compares with the latest canonical settlement observation, not with every observation ever seen.

## Design

### 1. Cheap due-candidate routing; no source-wide gate

Add a repository query that returns only compact due-candidate fields from `trade_lifecycle_cases` and `trade_lifecycle_timing_policies`, plus an O(1) per-case evidence mutation revision:

- case id, account, status, decision type, target manifest, observation start, pending deadline, and case `updated_at_ms`;
- settlement deadline, timing-policy schema, and calendar hash;
- a monotonic lifecycle evidence revision for cache invalidation;
- persisted derived reason/pairing hints when present.

The query must not deserialize account-wide lifecycle evidence or call `read_lifecycle_account_rows()`.
It must exclude absorbing `ledger_written`, `conflict`, and `superseded` cases at the SQL boundary and use an account-scoped partial due index, so idle-tick candidate work grows with active cases rather than all historical cases. `waiting_settlement_evidence`, `needs_review`, and any non-absorbing forward-compatible state remain eligible for the planner.

Build `case_scope_fingerprint.v2` from those fields. It is a control-cache invalidation token only; it is not evidence identity. Own the evidence revision beside `trade_lifecycle_evidence`: SQLite triggers atomically increment one row per affected case after INSERT, case binding/rebinding, or DELETE. Existing databases need no history-count backfill: an absent revision reads as zero, the fingerprint schema upgrade forces one reclassification, and every later evidence mutation changes the token. The compact query joins this revision by primary key and never counts or scans case evidence. A case/timing/evidence change invalidates any cached provider classification or backoff decision.

For each candidate, the due runner follows this order:

1. If an unchanged control row already classifies the case as `provider_required`, evaluate configuration, static capability, backoff, and claim eligibility before any full lifecycle read.
2. Otherwise invoke a refactored due planner with `observation_collector=None`. It may execute an existing local-only transition, return `not_due`, or return a typed `provider_observation_required` result without calling a provider. When an account has multiple ambiguous candidates, load one coherent account snapshot and derive all per-case plans from it; do not rebuild the same account evidence bundle once per case.
3. Persist `provider_required` only in source-local control state, bound to the current case-scope fingerprint. Do not mutate lifecycle business state merely to cache this classification.
4. If provider collection is eligible, acquire the first attempt claim, read one coherent account snapshot, and build the account history indexes once. Derive every eligible case context from those shared indexes, then acquire/revalidate each case claim and provider requirement before calling the collector. Reuse each derived context for reconciliation; do not rescan the full account history once per case or materialize the account twice.
5. If the revalidated case is local-only or no longer due, execute the local result and release/update the claim without a provider call.

This permits one full classification read for an ambiguous case, but unchanged blocked provider cases do only the compact candidate query on later ticks. A mixed account therefore cannot freeze local-only cases.
Each genuinely admitted semantic write still performs its own in-transaction lifecycle-generation compare-and-set. That correctness read is proportional to admitted writes and cannot reuse a pre-provider snapshot; the runtime must suppress any additional post-write display/read-model refresh because compact candidate refresh already closes the control transition.

### 2. Narrow collector capability and outcome contract

Introduce a settlement-only contract rather than a repository-wide capability framework:

```text
SettlementCollectorContract
  name = "settlement_observation"
  contract_version
  required_capability_keys[]

SettlementCapabilitySnapshot
  contract_version
  gateway_adapter_version
  provider_sdk_version
  capability_fingerprint
  capabilities[key] = supported | missing_static

SettlementAttemptOutcome
  kind
  source_id, account, case_id
  contract_version, capability_fingerprint
  reason_code, provider_code, error_class
  observation?                  # only for structurally valid observed results
```

The gateway adapter maps collector capability keys to provider methods. The due runner only consumes keys and typed states; it contains no method-name branches. Static preflight uses adapter/schema/version and method presence only, makes no provider request, and runs once at source startup. The fingerprint changes when the collector contract, adapter version, SDK version, or required method surface changes.

Attempt outcomes and decisions are:

| Outcome | Business evidence | Retry decision |
|---|---:|---|
| `observed_complete` | Admit once through ledger writer | Case normally becomes terminal |
| `observed_incomplete` | Admit a semantic transition once | 5m, 15m, 60m, then 6h cap while unchanged |
| `retryable_error` | None | 1m, 5m, 15m, then 60m cap; honor a longer provider `retry_after` |
| `unknown_error` | None | 5m, 15m, 60m, then 6h cap; never promote to permanent by count |
| `blocked_static` | None | No in-process retry until capability fingerprint/startup changes or operator reset |
| `blocked_account_explicit` | None | 24h re-probe; reset on scope/capability change or operator reset |
| `stale_generation` | None | Release claim and reclassify on the next normal tick |

`blocked_account_explicit` is legal only for a provider code that the adapter explicitly allowlists as unsupported/forbidden for this account and operation. Authentication expiry, 2FA, rate limit, timeout, provider unavailable, malformed response, and unrecognized exceptions are not permanent capability evidence. Until a real provider code is verified and mapped, it remains `unknown_error` or `retryable_error`.

Refactor settlement query receipts so they preserve `error_class`, normalized `provider_code`, and retry metadata. Raw error text remains diagnostic and is never used for park/retry or semantic identity. A transport/query failure does not masquerade as an observed incomplete business fact.

A test-only legacy `cash_flow_query` declaration is the historical `missing_static` fixture. A second synthetic capability key must prove that the scheduler behavior is driven by the declared requirement set. Neither key belongs to the production collector contract.

### 3. Bounded source-local attempt state

Use the existing per-source inbox SQLite file and add one overwrite-in-place row per `(source_id, account, case_id)` for `settlement_observation`:

```text
lifecycle_settlement_attempt_state
  source_id, account, case_id                   # primary key
  case_scope_fingerprint
  provider_input_scope_fingerprint
  collector_contract_version
  capability_fingerprint
  classification                               # unknown | provider_required
  outcome_kind, reason_code, error_class
  attempt_count, no_progress_count
  next_attempt_at_ms, last_attempt_at_ms
  last_semantic_fingerprint
  claim_id, claim_until_ms
  updated_at_ms
```

The same source-local DB owns one separate overwrite-in-place resource lease per
`(source_id, account)`:

```text
lifecycle_settlement_provider_batch_leases
  source_id, account                              # primary key
  claim_id, claim_until_ms, updated_at_ms
```

The batch lease serializes account-wide provider materialization for the full
multi-case batch; it carries no business classification or attempt outcome and
is deleted by an owner-checked release. Per-case attempt claims remain the JIT
provider-call authority and complete immediately after each case. Both lease
types use SQLite transactions, a monotonic renewal guard, and a minimum
120-second lease; abandoned ownership becomes reclaimable after expiry. Attempt
rows store the latest bounded status, not append-only history.

Historical control rows remain available for restart recovery and diagnosis, but the per-tick scheduler never lists them wholesale. Both the initial state lookup and terminal summary receive the current compact candidate IDs and account, and fetch only those composite-primary-key rows in bounded batches below SQLite's parameter limit. An empty candidate set yields an empty current summary without materializing historical rows. A just-completed candidate may remain in the summary for that tick; it disappears from the next tick once the ledger compact query excludes it.

A case-scope fingerprint change invalidates the cached classification and forces a no-provider replan, but does not by itself clear provider backoff. The replan computes `provider_input_scope_fingerprint.v1` from the frozen case/target/effective-pairing/timing/account binding that would be passed to the collector. Reset attempt/backoff counters only when that provider-input scope, the capability fingerprint, or the observed semantic fingerprint meaningfully changes. Unrelated lifecycle evidence may therefore force one reclassification read but cannot immediately authorize another provider call. Repetition alone never converts `unknown_error` into a blocked state. Repeated status logs are emitted only when the state/reason changes; ordinary ticks expose aggregate skipped/eligible counters. After a canonical write, refresh the compact candidate and store its post-write case-scope fingerprint with the outcome. Storing the pre-write fingerprint would make the newly inserted evidence look like external drift on the next tick and incorrectly bypass the first backoff interval.

The ordering is mandatory:

1. atomically claim in the control DB;
2. collect and classify;
3. for an observed result, call the canonical ledger writer;
4. only after the ledger transaction returns, update/release control state.

Crash behavior follows from that order:

- crash before the ledger write: the lease expires and the observation is retried;
- crash after the ledger commit but before control update: one later attempt may occur, and ledger semantic admission absorbs it;
- missing/recreated control row: one attempt may occur before the row is rebuilt;
- control-table failure in an otherwise usable inbox DB: no provider call is allowed, while local-only lifecycle processing continues; whole-inbox corruption remains service-fatal as documented under residual risks.

### 4. Versioned semantic projection

Add the pure projection `settlement_observation_semantic.v1`. It is an explicit allowlist, not a recursive “remove volatile fields” utility. Put the source-row alias normalizers, canonical semantic DTO/projection/hash, and legacy evidence adapter together in one I/O-free lifecycle-settlement evidence-contract module below both reconciliation and writer. The trade adapter calls that module when constructing a new observation, and the ledger writer calls the same module when validating new input or projecting legacy evidence inside its transaction. The module may know the stored `broker_settlement_observation` source schemas and field aliases, but it must not import the provider SDK, gateway, or `src.application.trades`; the ledger layer must not import `src.application.trades`.

The projection contains:

- schema version;
- case/account/broker/market/Futu-account binding;
- the frozen effective-pairing facts hash needed to interpret the observation, built from normalized business roles, deal keys, contracts, and remaining quantities rather than mutable case status, revision, generation, or evidence-row IDs;
- normalized contract identity;
- target contracts by lot and frozen preterminal remaining contracts by lot;
- anchor option deal key and anchor execution time;
- observation start, settlement deadline, `observed_after_settlement_deadline`, calendar hash, and normalized query window;
- sorted required source keys;
- for every required source: typed status, normalized provider code/error class, normalized query scope, relevant-row count, coverage complete, pagination complete, stale flag, fallback-cache flag, row-normalizer schema, and relevant semantic rows hash;
- canonical hashes for the normalized relevant rows while preserving duplicate-row multiplicity;
- canonically sorted stock-settlement candidates;
- broker-option-absent, projection-match, reservation-exclusive, competing-consumption, stock-present, and normal-order-present booleans;
- complete flag and sorted incomplete reason codes;
- the resulting evidence kind (`settlement_observation` or `expire_close`).

Rows use explicit source-specific allowlists and alias normalization before hashing:

- anchor option close: evidence/source identity, account binding, contract identity, contracts, price, event/received time, order ID, and clearing date;
- history deals: canonical deal/account/contract/side/quantity/price/trade-time/order/clearing fields used by anchor and stock-settlement matching;
- history orders: canonical order ID, broker-auto flag, and normalized order origin used by manual-versus-automatic classification;
- fresh positions: canonical account/contract identity and the quantity fields used by option-position absence;
- trading calendar: normalized date and day type;
- contract metadata: settlement style, security type, cutoff, cutoff source, and calendar hash.

Each normalizer maps accepted provider aliases to one canonical field name and ignores unknown SDK columns. It then applies the same case-bound relevance predicate used by the settlement decision:

- history deals retain only the option anchor match and stock-settlement candidates for the target underlying;
- history orders retain only rows for the anchor order ID;
- fresh positions retain only rows matching the target option contract;
- anchor, calendar, and contract metadata rows are already case/query scoped.

The provider's full result row count and full-row payload hash remain in the audit receipt but are excluded from semantic equality; otherwise unrelated account trading would create new settlement evidence. Coverage, pagination, freshness, normalized error class, and query scope still prove whether the filtered absence is usable. The projection uses only relevant-row count/hash.

A normalizer schema/version is part of the semantic projection. Scalars use the domain's canonical decimal, integer, timestamp, enum, account, and symbol representations; invalid business scalars make the result structurally incomplete rather than falling back to raw text. Canonical relevant rows are sorted by canonical JSON text. Sorting must not convert them to a set; duplicate rows and relevant-row count remain meaningful.

The projection excludes:

- exact top-level and per-receipt `observed_at_ms` values;
- raw error strings and stack traces;
- request/correlation IDs;
- transport latency, retry counters, log fields, and claim metadata.
- mutable lifecycle output fields such as case status, resolution revision, state fingerprint, notification state, and the admission head itself.

The exact timestamp remains in the first admitted evidence payload. Its deadline meaning is retained in the projection through `observed_after_settlement_deadline`; crossing the deadline therefore changes semantics even though the wall-clock value itself does not.

Compute:

```text
semantic_fingerprint = canonical_hash(settlement_observation_semantic.v1)
```

Do not include `lifecycle_generation_token` in this fingerprint. For a newly admitted event, the writer creates a deterministic evidence/source-event id from case id, semantic schema/fingerprint, the expected generation token, and the previous canonical settlement evidence id. Generation and predecessor make concurrent writes against one snapshot converge while still permitting a later meaningful `A -> B -> A` transition.

The evidence envelope stores `semantic_schema`, `semantic_fingerprint`, and the frozen semantic context used by the projection; the existing observation body remains available to current readers. Legacy evidence without this metadata is adapted by combining its stored observation fields with the current frozen case context only after case, target, anchor, timing, and effective-pairing facts are proven equivalent. Mutable output status/revision is never supplied to that adapter. If a required field cannot be reconstructed, compatibility fails closed as described below.

### 5. Atomic latest-semantic admission in the ledger writer

Add a single internal writer entry point used by both the issue and terminal settlement branches. It runs under the existing `BEGIN IMMEDIATE` transaction and performs:

1. load the case and require a non-empty expected lifecycle generation token;
2. recompute/validate the incoming semantic projection and fingerprint through the provider-independent ledger semantic module;
3. enforce the existing lifecycle-generation compare-and-set;
4. load the one-row canonical settlement admission head for the case;
5. when the head is absent, or its evidence ID is not the most recently inserted settlement evidence, seed/repair it from the newest `source_type=broker_settlement_observation` row ordered by `(created_at_ms DESC, rowid DESC)` and derive a fingerprint from a supported legacy payload when metadata is absent;
6. if the head fingerprint equals the incoming fingerprint, reuse the stored evidence and run only the idempotent state-coherence check; return `duplicate_semantic` with no new evidence, revision, allocation, or outbox;
7. otherwise assign the deterministic evidence id, insert the first-seen payload, advance the admission head, and execute the existing issue or terminal state transition in the same transaction.

Add the narrow table `trade_lifecycle_settlement_admission_heads`, keyed by `case_id`, with semantic schema/fingerprint, evidence ID, evidence creation time, and update time, plus a composite foreign key to the case-bound evidence row. It is a technical idempotency pointer: it is not part of lifecycle resolution, generation, revision, or notification state. Update it only inside the same writer transaction as evidence/state admission. Use `created_at_ms` plus SQLite `rowid` only to seed or repair the pointer after upgrade/version skew; normal comparisons use the explicit head and are therefore not ambiguous when multiple evidence rows share one millisecond. Add a `(case_id, source_type, created_at_ms)` lookup index if query-plan tests show the existing case index does not bound that one-row bootstrap. No historical row is rewritten or deleted. A supported legacy row with equivalent semantics is adopted as the canonical head and does not generate a post-upgrade duplicate.

If the latest legacy payload is malformed or cannot be projected, return a typed `legacy_semantic_unavailable` diagnostic and fail closed from automatic admission. Do not guess equality and do not scan thousands of older rows in the listener path. This case requires an explicit offline inspection before retrying that case.

The duplicate check is deliberately latest-state comparison, not a global unique semantic key. This keeps consecutive retries idempotent without suppressing a later reversion to a previously seen but newly meaningful state. The head mismatch check also handles rollback/version skew in which an older writer inserted a settlement row without updating the new pointer; a planned deployment must still prevent two application versions from writing concurrently.

The existing generation CAS, source-consumption claims, allocations, lifecycle state fingerprint, resolution revision, and notification-outbox creation remain authoritative. Extract transaction-scoped helpers as needed; do not call one public transactional writer from inside another transaction.

### 6. Configuration, status, and operator boundary

Add `trade_intake.settlement_observation.enabled`, defaulting to `true`, through the existing YAML authoring allowlist, normalization, validation, and per-source account mapping. It disables only provider collection. It cannot disable discovery, local due aging, backfill, holdings sync, or listener ingestion.

Expose per-source bounded status with:

- collector enabled/disabled;
- contract and capability fingerprints;
- counts by `provider_required`, blocked, backoff, claimed, and eligible;
- last state-change reason and timestamp;
- earliest next attempt;
- provider call and semantic-admission counters for the current process.

The listener's existing delivery-status and inbox-summary diagnostics must not
turn historical retention into permanent idle work. The ledger owns one
monotonic delivery-status revision advanced in the same transaction as changes
to lifecycle cases, evidence, timing policies, notification outbox rows,
delivery batches, or migration receipts. The inbox DB similarly owns one
summary revision advanced by every `trade_inbox` insert, update, or delete.
Each source listener may reuse its last successful snapshot only while the
corresponding revision is unchanged. It reads the revision both before and
after a rebuild and does not publish a cache entry if they differ. Cache token
and snapshot are published as one entry because heartbeat and push callbacks
may overlap. Time-sensitive fields such as overdue state, retry eligibility,
and age are rendered from the cached static snapshot on every heartbeat, and
dispatcher status is still refreshed independently on every call. A revision
read or rebuild failure clears the cache and reports the existing typed
`unavailable` status rather than escaping the listener status boundary.

Do not expose raw provider error text or credentials. Operator reset clears only the named source/account/case control row; it does not alter ledger evidence or lifecycle state. Adding a public reset command is not required for the first implementation if a process restart plus changed capability fingerprint provides the necessary static reset; any later CLI must be a separate reviewed write surface.

## Implementation slices

### S1 — Semantic contract and canonical admission

- Add the pure semantic projection and field-level tests.
- Add the settlement admission-head table plus its latest-evidence bootstrap query.
- Add the atomic settlement admission writer and route issue/terminal paths through it.
- Prove legacy latest-row compatibility, crash safety, and two-writer behavior before changing scheduling.

Primary ownership: an I/O-free `src/application/ledger/lifecycle_settlement_semantics.py`, `src/application/trades/close_reason_evidence.py`, `src/application/ledger/repository.py`, `src/application/ledger/writer.py`, and `src/application/trades/close_reason_reconciliation.py`. `src/application/trades/settlement_observation.py` supplies raw receipts to the shared normalizers; no ledger-to-trades or ledger-to-provider import is allowed.

### S2 — Typed collector outcomes and bounded attempt state

- Add the settlement-only capability/outcome contract.
- Preserve typed gateway errors through settlement receipts.
- Add source-local attempt state, atomic claims, fake-clock backoff, and transition-only logging.

Primary ownership: `src/application/trades/settlement_observation.py`, `src/infrastructure/futu_gateway.py`, `src/application/trades/inbox.py`, and a small settlement-attempt module if needed.

### S3 — Due routing, config, and status

- Add compact due candidates and case-scope fingerprinting.
- Exclude absorbing terminal cases with the account-scoped partial due index.
- Refactor per-case due planning so local-only work can run with no collector.
- Batch-index one coherent account snapshot for supported multi-case provider contexts.
- Apply capability/backoff only after a case is known to require provider observation.
- Wire the narrow configuration key and bounded status.
- Gate lifecycle-delivery and inbox-summary history scans behind their
  storage-owned mutation revisions while preserving dynamic heartbeat fields.

Primary ownership: `src/application/trades/auto_intake.py`, `src/application/trades/lifecycle_runtime.py`, `src/application/trades/close_reason_reconciliation.py`, `src/application/trades/account_mapping.py`, `src/application/config_yaml.py`, and `src/application/config_validator.py`.

Each slice is reviewed independently. Aggregate review occurs only after the mixed-case, crash, restart, and long-tick matrix passes.

## Required tests

### Routing and capability

- One account contains a local-only deadline case and a provider-required case. With the collector disabled or statically blocked, the local case advances; provider calls and settlement evidence writes are zero.
- After the provider-required classification is cached, at least ten 60-second ticks execute no account-wide lifecycle read for that unchanged blocked case.
- A test-only legacy `cash_flow_query` declaration and a second synthetic missing capability key produce the same `blocked_static` path without entering the production requirement set or adding scheduler method-name branches.
- Explicit allowlisted account unsupported, auth/2FA, rate limit, timeout, provider unavailable, malformed receipt, and unknown exception map to the prescribed classes. Repeated unknown errors never become permanent.
- A capability change resets eligibility. A case-scope change forces reclassification, but unchanged provider-input scope preserves the existing backoff.
- An unrelated lifecycle evidence insertion invalidates classification but cannot immediately authorize another provider call.

### Semantic projection

- Current-schema evidence must carry matching semantic schema, fingerprint,
  and frozen projection at both the evidence-envelope and embedded-observation
  boundaries. Missing or contradictory current metadata fails closed; only a
  fully metadata-free legacy payload enters compatibility projection.
- Only attempt timestamp changes: same fingerprint.
- Source row order changes: same fingerprint; duplicate rows remain counted.
- An unrelated account deal, order, or position row changes only the audit receipt: same fingerprint.
- An unknown SDK response column or a mutable lifecycle output status/revision changes: same fingerprint.
- Raw error text/request ID/latency changes: same fingerprint.
- Query window expands: new fingerprint.
- Deadline relation changes from before to after: new fingerprint.
- Stale to fresh, incomplete to complete, empty to row, coverage/pagination, normalized provider/error class, allowlisted source facts, target manifest, effective pairing, anchor, calendar, or business booleans change: new fingerprint.
- Lifecycle generation changes only because equivalent evidence was already admitted: same semantic fingerprint and no second write.
- A meaningful case scope change changes the semantic fingerprint.
- `A -> B -> A` produces three semantic transitions, while `A -> A` produces one.

### Atomicity and recovery

- Issue evidence and terminal evidence both use the new writer and are duplicate-safe.
- A healthy duplicate proves canonical evidence binding, lifecycle revision,
  allocation/resolution summary, target projection, and branch-specific status
  coherence before returning `duplicate_semantic`. Evidence-only legacy rows,
  missing terminal allocations, and mismatched issue status/reasons fail closed
  with `SettlementAdmissionStateIncoherent`; automatic recovery never replays
  or repairs business writes.
- Malformed canonical revision/allocation/projection fields and foreign-key
  violations are normalized to `SettlementAdmissionStateIncoherent` at the
  settlement writer boundary. The runtime records `unknown_error` with bounded
  backoff and clears the attempt claim; SQLite availability errors retain their
  infrastructure exception type.
- Every non-crash path after claim acquisition converges on the same atomic
  completion owner. Shared provider preparation failures back off without
  incrementing provider attempts; unexpected collector, reconciliation, and
  post-write refresh failures record a bounded `unknown_error` outcome and
  clear the owned claim. Ownership loss never clears another worker's claim.
- Lease-renewal thread startup is part of that owned boundary: startup failure
  calls no provider and completes the initial claim with backoff. Completion
  input normalization tolerates malformed persisted counters, and a minimal
  typed fallback can still clear the owned claim if normal update derivation
  itself fails.
- A source/account batch lease is acquired before the first provider account
  read and renewed through preparation and the complete multi-case loop. A
  concurrent batch owner or failed first per-case claim blocks materialization
  for the whole tick; the worker never falls through to another case. Per-case
  claims retain their independent monotonic guards and immediate completion.
- Crash before writer commit leaves no evidence/state/outbox and becomes retryable after lease expiry.
- Crash after writer commit but before control update permits one extra collection but no new evidence, revision, allocation, or outbox.
- Two workers with the same generation and semantic result produce one canonical transition; the loser receives stale-generation or duplicate-semantic without retry storm.
- Two evidence rows inserted in the same millisecond cannot make the admission head regress or cause a duplicate transition.
- A latest legacy timestamp-based observation with equivalent semantics is reused without a new row. A malformed latest legacy payload fails closed with the typed diagnostic.
- A simulated old-version write that leaves the admission head stale is detected and reseeded before comparison.
- Deleting a healthy control row permits at most one extra provider attempt; recreating the process preserves backoff. A control-table failure in an otherwise usable inbox DB makes zero provider calls and does not block local-only due work. Whole-inbox corruption retains the existing service-level fail-closed behavior.

### Resource and regression

- Seed at least 2,100 equivalent legacy observations for two provider-required cases. Initial classification materializes the account at most once; ten later blocked ticks perform zero provider calls, zero semantic writes, zero resolution-revision/outbox changes, and zero account-wide evidence materializations.
- Restore supported capability for those two cases and prove the provider path builds the full account-history index exactly once, makes one bounded attempt per eligible case, reuses each context for canonical reconciliation, and performs no account read beyond the shared snapshot plus one transactional generation CAS per admitted write.
- With two overlapping workers and two eligible cases, start the loser both
  before preparation and synchronously after the leader case completes. In both
  windows it performs zero account-wide materializations and zero provider
  calls while the owner still processes the second case. Advance the monotonic
  clock past the original 120-second lease and prove competing batch/per-case
  claims still fail; ownership loss must never delete the new owner.
- With many `ledger_written`/`conflict`/`superseded` cases and a small active set, prove the compact query returns only active cases and uses the partial due index.
- Mutate lifecycle evidence through insertion, old-timestamp backfill, case binding/rebinding, and deletion; prove the per-case revision always advances and the compact SQL contains no evidence `COUNT` or evidence-history scan.
- Seed at least 1,200 stale terminal attempt rows; prove scheduler state lookup and summary return only current candidate IDs through the composite primary key, and an empty candidate set returns zero current counts.
- Seed a large lifecycle/status history and prove the first delivery-status
  refresh performs one authoritative rebuild, ten unchanged heartbeats perform
  none, time-dependent deadline fields still advance, dispatcher status still
  refreshes, and any relevant table mutation causes exactly one new rebuild.
  Simulate a revision change during the rebuild and prove the snapshot is not
  cached; simulate a revision read failure and prove status becomes
  `unavailable` without reusing stale data.
- Seed at least 1,200 handled inbox rows and prove ten unchanged summary calls
  aggregate once. Enqueue, status update, and delete must each advance the
  revision and cause one rebuild, while an idempotent duplicate enqueue causes
  neither. A revision change during aggregation must prevent cache publication.
- Advance a fake clock through 24 hours and assert the exact bounded call counts for retryable, unknown, account-blocked, and unchanged-observation schedules.
- Listener ingestion, backfill discovery, holdings sync, local deadline review, and normal supported settlement resolution retain existing behavior.
- Config authoring accepts only `trade_intake.settlement_observation.enabled` in this slice and continues rejecting unrelated write-policy keys.

Run focused settlement/lifecycle/auto-intake/config tests, Ruff on changed Python, dependency-graph validation, `git diff --check`, and the full repository test suite.

## Acceptance and later recovery

Source acceptance requires all deterministic tests above and an aggregate DeepReview with no unresolved high or severe finding.

A later production recovery requires separate release/deployment/config/start authorization. Before enabling the service, inspect the deployed code fingerprint, source configs, control DB path, ledger backup/headroom, and the still-stopped unit. Do not delete the existing large evidence history.

For the first ten natural due ticks after an authorized restart, collect read-only evidence that:

- blocked provider candidates make zero provider calls and zero account-wide evidence materializations;
- settlement evidence rows, affected lifecycle revisions, allocations, and notification outbox remain unchanged unless a genuinely new semantic observation is admitted;
- local-only due cases still produce their expected outcomes;
- control status shows the typed reason and next eligibility time;
- trade-intake CPU is no longer sustained near the incident level. Sample every five seconds and require median below 10%, p95 below 25%, and no three consecutive samples above 50% while no other documented task is active.

If capability is later restored, canary one account/case first. A repeated equivalent observation must report `duplicate_semantic` and leave evidence/revision/outbox counts unchanged.

Rollback is application-version rollback. The added source-local control table, optional evidence metadata, and read index are backward-compatible and may remain; rollback must not delete ledger rows. If the old version is restored, keep trade intake stopped until its former unbounded settlement loop is otherwise gated.

## Residual risks

- The historical evidence remains large. An explicitly requested full lifecycle read may still be slow; bounded readers or data compaction are separate work units.
- A supported provider call still runs under the current shared process topology. The lease/backoff bounds frequency, but service isolation is a separate hardening decision.
- A malformed latest legacy settlement payload fails closed for that case and needs offline inspection; automatic code does not guess or scan the full history.
- Whole-inbox SQLite corruption can stop the listener before lifecycle routing, as it does today; isolating all control and intake storage is not part of this collector fix.
- This contract covers settlement-observation failures across its required sources. Other collectors gain no protection until they explicitly adopt equivalent capability, control, and canonical-admission ownership.

## Completion evidence

The work unit is complete only when code review can show all of the following together:

- the provider gate is branch-local and a mixed local/provider account test passes;
- error classification is typed and unknown never becomes permanent;
- control-state loss cannot create or suppress a canonical business transition;
- semantic projection is field-level, versioned, and passes the change/no-change matrix;
- the writer rejects mismatched frozen projections and incoherent duplicate
  business state without adding evidence, allocations, revisions, or outbox;
- issue and terminal paths share one atomic latest-semantic admission owner;
- the 2,100-row regression proves blocked ticks no longer revisit the inflated account read path;
- overlapping workers elect one renewable source/account provider-batch owner
  that bounds account-history materialization through every per-case handoff;
- compact idle reads use evidence mutation revisions and current candidate-scoped control queries rather than historical counts/scans;
- unchanged listener heartbeats and inbox retry ticks read O(1) mutation
  revisions and reuse snapshots instead of rescanning retained history;
- no production service, data, release, or notification was changed by implementation validation.
