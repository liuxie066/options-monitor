# Gateflow Plan — lifecycle receipt batching

## Gate state

- Work unit: `lifecycle-receipt-batching`
- Base: `origin/main@51275d59` (`v1.8.5`)
- Current gate: accepted plan commit
- Next gate: S1 implementation
- Status: planreview re-review passed with classified residual risks
- Design source: confirmed goal artifact plus current source/schema/tests; no separate design document

## Goal and motivation

Lifecycle reconciliation correctly persists one immutable notification intent per case transition, but the current dispatcher claims and sends one outbox row per external call. A burst of row-level reconciliation can therefore become a burst of Feishu/other channel messages even though the intended human action is one review session.

The implementation keeps row-level intent and audit semantics, while introducing one durable external-delivery batch that binds all eligible intents sharing the same resolved provider/channel/target. This moves batching to the external delivery ownership boundary without weakening lifecycle facts or hiding member-level audit evidence.

## Direct code evidence and owners

- `src/application/ledger/writer.py` creates a transition-specific immutable outbox intent inside the lifecycle state transaction. That remains the fact/audit owner.
- `src/application/ledger/repository.py::_ensure_notification_outbox_v2()` and notification repository methods own SQLite durability and compare-and-set writes.
- `src/application/trades/lifecycle_outbox.py` owns delivery claim, attempt, recovery, ambiguity freeze and manual resolution semantics.
- `src/application/trades/receipt.py` owns lifecycle receipt rendering, route resolution and provider adaptation.
- `src/application/trades/auto_intake.py` currently invokes `dispatch_notifications_once()` from every source loop every five seconds, filtered by account. This is the storm-producing delivery ownership boundary.
- `src/application/notification_delivery_adapter.py` already accepts a caller-provided `idempotency_key`; lifecycle delivery currently does not pass one.

## Scope and non-goals

### In scope

- additive SQLite batch storage and nullable member binding;
- deterministic batch formation, state transitions and recovery;
- deterministic single/multi-member rendering and stable transport identity;
- one process-level dispatcher shared by all configured source accounts;
- batch-aware CLI inspection/reconciliation and delivery diagnostics;
- deterministic migration, failure, concurrency and integration tests;
- operator documentation for the changed lifecycle receipt contract.

### Out of scope

- lifecycle decision/allocation/reconciliation policy;
- Daily Brief, normal trade-intake receipt, auto-close and assistant-reply delivery;
- arbitrary notification batching framework or general message bus;
- new public config keys for timing/limits in this work unit;
- distributed scheduling or cross-host locks;
- production sends, production config, ledger mutation, Release, deployment or remote apply.

## Data model and migration contract

### Existing intent row

Add nullable `delivery_batch_id TEXT` to `trade_lifecycle_notification_outbox` and an index on `(delivery_batch_id, created_at_ms, outbox_id)`. It is assigned once and is not part of the intent identity/hash. Existing rows retain all state and payload fields unchanged.

The member row remains the per-transition audit surface. Binding atomically changes every non-terminal member to a new fail-closed sentinel status `batched`. While the batch is pending, claimed, send-started or retryable-explicit-failed, the member stays `batched`; only the batch owns those mutable delivery states. Old code does not select `batched`, so rollback cannot reopen bound rows as per-line sends.

When a batch reaches a frozen/terminal outcome, the same transaction projects that outcome to all members: `confirmed`, `accepted`, `unknown`, or exhausted `explicit_failed`. Exhausted failure also writes member `attempt_count=3` and `next_attempt_at_ms=NULL`. Provider receipt authority remains on the batch; member inspection resolves and displays the owning batch rather than duplicating an external receipt per member.

### New delivery batch

Create `trade_lifecycle_notification_delivery_batches` with:

- immutable identity: `batch_id`, `route_fingerprint`, `provider`, `channel`, `target_fingerprint`, `renderer_version`, `payload_json`, `payload_hash`, `member_count`, `first_intent_created_at_ms`, `last_intent_created_at_ms`, `created_at_ms`;
- mutable delivery state: `status`, `claim_id`, `claimed_at_ms`, `send_started_at_ms`, `attempt_count`, `next_attempt_at_ms`, `last_error`, `provider_message_id`, `provider_receipt_json`, `updated_at_ms`, `confirmed_at_ms`.

No raw target is persisted. `target_fingerprint` is SHA-256 of the normalized target; `route_fingerprint` is SHA-256 of canonical provider/channel/target components. The current raw route is resolved only at dispatch and must reproduce the frozen fingerprint before provider I/O.

The frozen batch payload contains ordered member envelopes with `outbox_id`, transition identity/revision, timestamps and the existing immutable member payload. A batch insert and all member bindings happen in one `BEGIN IMMEDIATE` transaction; any missing/already-bound member aborts the transaction.

### Additive migration rules

- New table/column/index creation is idempotent.
- Existing `suppressed`, `confirmed`, `accepted`, `unknown`, `batched` and exhausted `explicit_failed` rows are never rebound automatically.
- Existing unbound `pending`, and retryable `explicit_failed`, may enter the new batching path; the batch inherits the maximum prior member attempt count so cutover cannot exceed three total attempts.
- Existing v1/v2 upgrade tests are extended to prove provider receipt, status and payload preservation plus a null `delivery_batch_id`.
- Binding uses one repository method whose update predicate requires `delivery_batch_id IS NULL` and status in `pending/explicit_failed`, then sets `delivery_batch_id` and `status='batched'` together. Binding cardinality must equal the frozen member count or the whole transaction rolls back.
- Rolling back to v1.8.5 remains read-compatible and fail-closed for bound work because its claim predicate only selects `pending/explicit_failed`. It cannot finish batches, so forward upgrade is required to resume them, but it cannot recreate the storm. No existing lifecycle fact data is rewritten.

## Route and eligibility contract

The process-level dispatcher resolves the effective notification route from the same runtime config used by the current sender. It builds an allow-set from each source's explicit `account` plus every normalized value in its `account_mapping`; for each bound account it applies the existing `source.receipt or trade_intake.receipt` precedence. A batch may contain members from any enabled account when their provider/channel/target fingerprints match. Disabled-account rows remain visible and unbound; they are not silently suppressed. Tests use the repository's current multi-source config fixture to pin this precedence.

Current OM runtime has one effective notification route per config. If source/account-specific route resolution is discovered, the planner must receive one route snapshot per account and group by fingerprint; it must not fall back to account grouping. This is a stop condition if it cannot be expressed without new config semantics.

## Batch formation contract

Constants owned by `lifecycle_outbox.py`:

```text
QUIET_WINDOW_MS = 10_000
MAX_BATCH_WAIT_MS = 60_000
TARGET_SEND_INTERVAL_MS = 60_000
MAX_DISPLAY_ITEMS = 12
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MS = (60_000, 300_000)
RENDERER_VERSION = trade_lifecycle_batch.v1
```

Within one `BEGIN IMMEDIATE` transaction, the planner:

1. excludes bound rows and every non-retryable state;
2. filters to allowed accounts and due `pending`/`explicit_failed` rows with attempts below three;
3. orders rows by `(created_at_ms, outbox_id)`;
4. waits while both `now - newest_created < 10s` and `now - oldest_created < 60s`;
5. refuses to form a second active batch for the route;
6. enforces the last `send_started_at_ms` route budget before forming a new batch;
7. freezes every eligible member for the route into one batch, with no member-count split;
8. derives a provider-safe `batch_id` as `tlb_` plus 32 lowercase hex characters from renderer version, route fingerprint and the sorted complete member outbox IDs;
9. inserts the batch and atomically binds every member while changing its status to `batched`.

New intents arriving after this transaction stay unbound for the next batch. The 12-item limit is only a rendering limit and never a membership limit.

## Delivery state machine

```text
pending -> claimed -> send_started -> confirmed
                                  -> accepted   (frozen)
                                  -> unknown    (frozen)
                                  -> explicit_failed -> claimed (due, attempts < 3)

stale claimed before send -> pending
stale send_started        -> unknown (frozen)
```

- Batch claim, send-started and retryable failure transitions update only the batch; bound members remain `batched`. Confirmed/accepted/unknown/exhausted-failure completion and stale send-started recovery atomically settle the batch plus every member to the same terminal outcome.
- Stale batch claim recovery changes only the batch from `claimed` to `pending`; members remain `batched`.
- A batch attempt increments once, regardless of member count.
- Every actual provider call passes `batch_id` as the lifecycle transport idempotency key. All explicit-failure retries reuse it.
- Explicit failures use 60 seconds then 5 minutes backoff. Attempt three remains terminal `explicit_failed` with no next due time.
- Exceptions after durable `send_started` are ambiguous and settle the entire batch to `unknown`; no automatic resend occurs.
- `accepted` may be manually changed to `confirmed` or `unknown`; `unknown` may be manually changed to `confirmed` or explicitly resent.
- Manual resend keeps the original batch frozen and transactionally creates one compensating intent per original member, each with the next delivery revision. Those new intents are later aggregated into one new batch.

The dispatcher must re-check the route send budget during claim as well as planning so a stale pre-created pending batch cannot bypass the one-per-60-second rule.

## Rendering and provider contract

- One display representative is selected per case by highest `resolution_revision`, then latest creation time, then deterministic transition precedence (`conflict`, `needs_review`, `resolution_corrected`, `resolution_confirmed`, `option_leg_closed`), with the full membership still retained and settled.
- Representatives are sorted by severity, account, symbol, expiration, strike, case ID and outbox ID.
- A single representative delegates to the existing `build_trade_lifecycle_notification_message()` so the current receipt format remains byte-for-byte stable apart from transport metadata not rendered in the message.
- Multiple representatives render one Markdown-friendly Chinese digest with total intent count, case count, accounts, up to 12 rows and `另有 N 项` when truncated.
- Missing member display fields render explicit `-`/`待确认`; no member may disappear because of malformed optional data.
- Before calling the adapter, the sender resolves the current route and compares provider/channel/target fingerprints with the frozen batch. Missing/mismatched route is an explicit pre-acceptance failure.
- The selected adapter receives `idempotency_key=batch_id`; normalization continues to determine confirmed/accepted/explicit failure/ambiguous outcome.
- The lifecycle sender owns a narrow fail-closed classifier:
  - `delivery_confirmed` or confirmed provider status -> `confirmed`;
  - `command_ok` without confirmation -> `accepted`;
  - route preflight failure, adapter-declared pre-I/O failure, HTTP 4xx, or a provider business rejection with a received response and `ambiguous_send=false` -> `explicit_failed`;
  - exception, timeout, transient HTTP attempt, fallback ambiguity, `ambiguous_send=true`, or missing proof of non-acceptance -> `unknown`.
- The batch provider receipt records the normalized classifier inputs (`http_status`, provider code, `ambiguous_send`, fallback/idempotency evidence) and decision. No broad shared-adapter contract is introduced unless implementation proves an existing identical owner.

## Runtime ownership and scheduling

Introduce one `LifecycleReceiptBatchDispatcher` owned by `auto_intake.main()` for the lifetime of the process. It is started only when writes are enabled and at least one effective receipt configuration is enabled. It polls with a cancellable one-second wait and closes before repository/runtime teardown.

The dispatcher never holds the shared `process_lock` across provider I/O. Planner/claim, send-started and completion each use their own short SQLite `BEGIN IMMEDIATE` transaction. The provider call occurs after durable `send_started` and outside every SQLite transaction and `process_lock`; completion then uses claim-ID CAS. This lets trade push/inbox and lifecycle fact writes proceed while notification delivery is slow.

Remove lifecycle notification dispatch from `_run_listener_source_loop()`. Source threads continue to create intents and run lifecycle reconciliation, but cannot call the provider. This makes cross-account batching independent of source loop timing and ensures one process-level sender owner.

The dispatcher performs one batch attempt per poll. The durable target budget, not thread timing, is authoritative. On restart it first recovers stale batch states and then resumes due work.

## CLI and observability contract

- `lifecycle receipts inspect` accepts exactly one of `--outbox-id` or new `--batch-id`. Member inspection includes its batch; batch inspection includes ordered member summaries.
- `lifecycle receipts reconcile` accepts the same identity choice. `--outbox-id` remains valid for legacy unbatched rows and single-member batches. It refuses multi-member batch mutation and instructs the operator to use `--batch-id` so one row cannot accidentally mutate a group.
- `lifecycle receipts dispatch` becomes batch-aware. Its dry-run previews eligibility/window/rate-gate without binding or sending. Applied dispatch is global; an account-scoped applied dispatch is refused because it would split same-route delivery semantics.
- `_lifecycle_delivery_status()` advances to `trade_lifecycle_delivery_status.v2` and adds: unbound eligible count/oldest age, batch status counts, oldest unknown batch, total batched member count, confirmed/accepted `messages_avoided`, and dispatcher last-result/error when available. Existing case/evidence/outbox fields remain.
- No raw route target is printed or stored; only provider/channel and fingerprints are visible.

## Implementation slices

### S1 — Durable batch storage and atomic state machine

- **Objective**: establish schema, immutable binding, planner, CAS transitions, retry/recovery and manual batch reconciliation without changing runtime sender ownership.
- **Allowed production files**: `src/application/ledger/repository.py`, `src/application/ledger/api.py` only if export boundaries require it, `src/application/trades/lifecycle_outbox.py`.
- **Allowed test files**: `tests/test_lifecycle_redesign_contracts.py` and a new focused `tests/test_trades_lifecycle_batch_outbox.py` if separation materially improves reviewability.
- **Exact changes**:
  1. add migration/table/row serializers and repository methods;
  2. add route snapshot/batch identity pure helpers and batch planner preview/create paths;
  3. implement batch recovery/claim/send-started plus atomic terminal completion, with members held in `batched` until terminal;
  4. implement batch-level manual resolution and compensating-intent creation;
  5. keep legacy unbatched functions only where CLI migration compatibility needs them, and prevent them from claiming bound rows.
- **Required tests**: migration preservation, 24-member atomic binding, quiet/max window, target rate gate at plan and claim, no split, concurrent planner/claim winner, confirmed all-member settlement, explicit failure attempt ceiling/key stability metadata, ambiguous freeze, stale recovery, multi-member manual safety, compensating resend, old suppressed/confirmed non-reactivation, and a v1.8.5-equivalent selection predicate proving `batched` members cannot be claimed after rollback.
- **Completion signal**: repository/state-machine tests prove all transitions without any provider call.
- **Stop conditions**: inability to bind atomically with existing SQLite authority; need to mutate immutable transition payloads; evidence of another process writing delivery state.

### S2 — Batch renderer, sender, CLI and diagnostics

- **Objective**: make the external payload and operator surfaces batch-aware while keeping provider behavior fail-closed.
- **Allowed production files**: `src/application/trades/receipt.py`, `src/interfaces/cli/option_positions.py`, `src/application/trades/auto_intake.py` only for the pure status projection, plus S1 files for directly exposed repository queries.
- **Allowed tests**: `tests/test_trades_receipt.py`, `tests/test_option_positions_cli.py`, `tests/test_lifecycle_redesign_contracts.py`, and the focused batch test file.
- **Exact changes**:
  1. deterministic representative selection and digest renderer;
  2. route fingerprint preflight and `idempotency_key=batch_id` provider call;
  3. batch-aware inspect/reconcile/dispatch dry-run contract;
  4. delivery status v2 counters and no-sensitive-target assertions;
  5. update CLI/operator documentation.
- **Required tests**: byte-stable single member, 24-member/top-12 digest, deterministic ordering/collapse, route mismatch no-send, provider 4xx/rejection -> explicit retry, transient/timeout/fallback ambiguity -> unknown/no retry, stable idempotency across retries, multi-member CLI refusal by outbox ID, batch-level dry-run/no-write and diagnostics counts.
- **Completion signal**: all provider calls in tests are fakes; message and CLI contracts are deterministic and member-complete.
- **Stop conditions**: adapter does not support stable caller keys for an active provider; current CLI identity cannot be extended without breaking unrelated commands.

### S3 — Global dispatcher cutover and storm regression

- **Objective**: remove per-source delivery ownership and prove one process-level cross-account dispatcher.
- **Allowed production files**: `src/application/trades/auto_intake.py`, new narrow `src/application/trades/lifecycle_batch_dispatcher.py` if needed, lifecycle receipt docs.
- **Allowed tests**: `tests/test_trades_auto_intake_cli.py`, `tests/test_trades_auto_intake_restart_policy.py`, `tests/test_trades_auto_intake_audit.py`, focused batch tests.
- **Exact changes**:
  1. construct/start/close one dispatcher in `main()` without wrapping provider I/O in `process_lock`;
  2. remove the five-second per-source outbox dispatch block and account filter;
  3. preserve listener stop/restart semantics and expose dispatcher result through status refresh;
  4. execute the historical 15+9 same-route fixture through the real batch dispatcher with one fake sender call.
- **Required tests**: single- and multi-source lifecycle ownership, orderly cancellation, no source-local send call, same-target `lx`+`sy` one call, next intent held until target budget expires, no dispatch when dry-run/disabled, source-level receipt enable precedence, and a blocking fake sender proving an independent ledger write can complete before delivery returns.
- **Completion signal**: 24 members across two accounts produce one sender call and one confirmed batch; existing listener restart tests pass.
- **Stop conditions**: global dispatcher cannot share current SQLite/process-lock lifetime safely, or a source-specific route cannot be represented by the confirmed grouping contract.

## Validation commands

Use the repository Python 3.12 contract because the checked-in `.venv` may not contain pytest:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_batch_focused python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_trades_lifecycle_batch_outbox.py \
  tests/test_lifecycle_redesign_contracts.py \
  tests/test_trades_receipt.py \
  tests/test_option_positions_cli.py \
  tests/test_trades_auto_intake_cli.py \
  tests/test_trades_auto_intake_restart_policy.py \
  tests/test_trades_auto_intake_audit.py

PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_batch_related python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_trade_event_ledger_long_lifecycle.py \
  tests/test_combo_yield_lifecycle.py \
  tests/test_positions_maintenance_receipt.py \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py

PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_batch_full python3.12 -m pytest -q -p no:cacheprovider
python3.12 -m ruff check <changed-python-files>
python3.12 scripts/generate_dependency_graph.py
python3.12 scripts/generate_dependency_graph.py --check
git diff --check
```

Required assertions, not merely green commands: one sender call for 24 members; atomic all-member settlement; stable batch ID on retries; no retry of unknown/accepted; legacy terminal rows unbound; no source-local dispatch path remains.

## Rollout, rollback and safety

- This Gateflow work unit ends at a passing Draft PR. Release and production upgrade remain separate explicit authorization boundaries.
- No live notification canary is run in this task. Tests use fake adapters and temporary SQLite ledgers.
- A code rollback leaves non-terminal bound members in unknown-to-old-code `batched`, so old dispatcher selection is fail-closed. Old code cannot progress those batches; restore a batch-aware version for forward recovery. The additive schema itself need not be removed.
- Before any later deployment, stop the listener, take a WAL-safe ledger snapshot, upgrade code, start one service instance, verify v2 delivery status and only then allow real delivery. That future operation requires explicit authorization.

## Risks and classified residuals

- **Cross-process duplicate dispatcher — deferred/blocking for rollout**: current scope guarantees one owner inside one process. Before production apply, service topology must prove a single active listener instance. If not, add a lease owner in a separate work unit.
- **Route configuration changes with queued unbound rows — accepted**: unbound intent has no prior external route commitment; it joins the route active when the batch freezes. Once frozen, mismatch fails before provider I/O.
- **Continuous event stream — fixed here**: the oldest-member 60-second deadline forces a batch; new arrivals after the freeze wait for the next target budget.
- **Digest hides individual detail — fixed here**: display truncation never changes membership, full intent/batch evidence stays inspectable, and counts are explicit.
- **Mixed-version rollback — mitigated**: `batched` blocks old per-row claim. Old/new concurrent writers remain unsupported; forward repair is required to resume batch delivery.

## Docs decision

Update the lifecycle receipt/operator section in `docs/AGENT_WIKI.md` (or its current owning document discovered by call-site search) because CLI identity, batch state and manual resolution semantics are operator-facing. Do not modify VERSION/CHANGELOG until a separately authorized release.

## Open questions

None blocking in the initial plan. Planreview must specifically challenge account-to-route ownership, transactional target rate enforcement, member/batch status synchronization, manual multi-member recovery and mixed-version migration.

## Completion report

Report accepted plan/review artifacts and commits; per-slice files/tests/review findings; aggregate and PR review outcomes; exact 24-row storm proof; branch/PR URL; pending release/production boundaries; and every residual risk with owner/destination.
