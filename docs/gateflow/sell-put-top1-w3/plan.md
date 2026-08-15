# Gateflow Plan — Sell Put Top1 W3

- Gate: `plan`
- Work unit: `sell-put-top1-w3`
- Branch: `feat/sell-put-top1-w3`
- Base: `origin/main@baa3e363`
- Goal contract: `docs/gateflow/sell-put-top1-w3/goal-confirmation.md`
- Artifact path: `docs/gateflow/sell-put-top1-w3/plan.md`
- Current gate: draft pending PlanReview

## 1. Goal and measurable completion

W3 adds one production-grade persistence/write-authority seam for the Sell Put Top1 loop. It must preserve phase, authorization, hidden-window ownership, terminal intent, and projection recovery across process restarts without evaluating strategy results or reading providers.

Completion means the synthetic acceptance matrix in §14 passes, the store remains the sole state authority, terminal bytes recover after every injected crash boundary, public reads reveal no hidden intermediate result, and Kimi finds no unresolved accepted issue in slice, aggregate, and PR reviews.

## 2. Non-goals

- No corpus, recommendation-point copying, research evaluator, leader calculation, hidden economics, outcome job/table, metrics, adoption, LLM, Prompt, CLI, Agent tool, timer, service profile, release, deploy, or real experiment.
- No Candidate Engine, scheduler, notification, ledger, config, or broker changes.
- No ORM, repository protocol, factory, workflow engine, event bus, queue, feature-flag platform, capability registry, or multi-store transaction coordinator.
- No empty future tables for corpus, research results, validation rows, observations, or outcomes.
- No duplicate raw point/candidate/result storage. W3 stores identities, refs, hashes, counters, and compact command facts only.

## 3. Existing owners reused

- `validate_experiment_spec()`, `build_research_spec_sha256()`, and `build_validation_spec_sha256()` remain the only ExperimentSpec/hash policy.
- `strategy_lab_top1_available()` remains the exact maintainer gate parser; W3 imports it and does not create a second environment rule.
- `attach_artifact_provenance()` and `artifact_content_sha256()` remain the content-hash contract for generation terminal payloads.
- The JSON formatting already used by `shadow_replay.common.write_json()` becomes one public pure `render_json_text(payload) -> str`; `write_json()` delegates to it. No second renderer is added.
- `private_storage` remains the sensitive SQLite path/permission owner.

## 4. Files and dependency direction

### Production

- New `src/infrastructure/strategy_lab/__init__.py`.
- New `src/infrastructure/strategy_lab/experiment_store.py`.
- New `src/application/strategy_lab/top1/lifecycle.py`.
- New `src/application/strategy_lab/top1/terminal_projection.py`.
- Modify `src/application/shadow_replay/common.py` only to extract `render_json_text()` from the existing `write_json()` body.

### Tests/docs

- New `tests/test_strategy_lab_top1_store.py`.
- Modify `tests/test_strategy_lab_top1_architecture.py` with exact import guards.
- Regenerate `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` only for actual edges.
- Add Gateflow and review artifacts.

### Dependency rules

```text
experiment_store.py -> stdlib/sqlite3 + src.infrastructure.private_storage only
terminal_projection.py -> stdlib + existing shadow_replay provenance/renderer
lifecycle.py -> W1B contracts + M2 availability + store + terminal_projection
production tick/M2 -> MUST NOT import lifecycle or store
```

The store never imports `domain/` or `src.application`; application policy never appears in SQL triggers.

## 5. SQLite schema and migration

Schema component is `sell_put_top1_experiment_store`, version `1`. The store exposes `schema_state()` without creating a file and `migrate(migrated_at_utc)` as an explicit write.

Migration accepts:

- a missing/empty database;
- a synthetic version-0 database containing only the schema metadata table;
- an already valid version-1 database idempotently.

Any unknown version, missing v1 table/index, invalid foreign-key layout, or corrupt database fails closed as `schema_unsupported`; it is never silently rebuilt.

Tables:

### `strategy_lab_schema`

`component` primary key, `schema_version`, `migrated_at_utc`.

### `strategy_lab_features`

One row per `(market, account)` with `user_opt_in` (`0|1`), last actor/time, and state version. Absence means false. This table never stores maintainer availability or environment values.

### `strategy_lab_experiments`

One row per experiment with only state/query/CAS fields:

- identity: experiment/topic/market/account/strategy family;
- one current canonical ExperimentSpec JSON plus research/validation spec hashes and compact initial provenance JSON;
- `draft|research|validation|concluded`, nullable research/validation progress, blocked reason, completed validation partition count;
- independent research/validation authorization status/hash/actor/time;
- research leader/receipt binding and proposed hidden commitment binding;
- experiment terminal intent/reason/scope/time/partition and final outcome status;
- final receipt request/published event IDs plus ref/content/file hashes;
- created/updated timestamps and `state_version`.

Exact CHECK constraints enforce all enumerations. The only active-slot index is exactly:

```sql
UNIQUE(market, account, strategy_family)
WHERE terminal_mode IS NULL
  AND phase = 'validation'
  AND validation_progress = 'collecting_decisions'
```

Research rows never match it.

### `strategy_lab_generations`

One row per `(experiment_id, generation_kind)` for `research|hidden|outcome` with:

- `open|terminal`, monotonic revision, last revision ref/file hash, frozen-row content hash;
- one immutable terminal-request event ID/mode/ref/content/file hash;
- one terminal-published event ID and timestamps.

No row is created for a not-yet-started generation. A generation receives at most one terminal request, so completed/aborted cannot coexist.

### `strategy_lab_hidden_commitments`

Exactly 20 immutable date-occupancy rows are inserted only in the transaction that starts validation. Each row stores experiment/scope, commitment hash, and one canonical trading date; the table has a unique constraint on `(market, account, strategy_family, trading_date)`. The canonical commitment JSON, ref, and content/file hashes remain stored once on the experiment and request event rather than repeated 20 times. Rows are never deleted or released.

This means a proposed/locked window is not consumed, while any validation that actually starts consumes all 20 committed dates even if later aborted.

### `strategy_lab_events`

Append-only rows with deterministic event ID, experiment/generation scope, event type, semantic subject key, caller idempotency key, actor/time, and compact canonical payload JSON. Requested events are never updated. Publication creates a separate `terminal_projection_published` event and CAS-updates the owning row.

Every mutation defines one natural subject before opening its transaction: validation point uses experiment plus point ID, partition uses experiment plus trading date, generation/experiment terminal uses its target, and singleton transitions use experiment plus transition name and bound hash/version. Natural facts are unique on `(event_type, subject_key)`. A caller idempotency key is unique inside its command scope and may only replay the same canonical request payload; the same key with different bytes conflicts. State CAS and these unique keys are both required, so different caller keys cannot duplicate the same fact.

The event table is the minimal audit/outbox; there is no second outbox table. Terminal-request payloads are capped at 8 KiB. No event embeds ExperimentSpec, candidate rows, daily metrics, or source artifacts already stored by reference.

All writes use one connection, `PRAGMA foreign_keys=ON`, `busy_timeout=5000`, and `BEGIN IMMEDIATE`. Default rollback journal is retained; W3 does not enable WAL.

## 6. Canonical hidden commitment

`build_hidden_window_commitment()` returns an exact `sell_put_top1_hidden_window_commitment.v1` object containing:

- experiment ID, `HK`, lowercase account, `sell_put`;
- exactly 20 strictly increasing canonical ISO trading dates, with start/end equal to the first/last;
- market-calendar version;
- fixed selector `official_scheduled_sell_put.v1` and capture schema `recommendation_point.v1`;
- challenger variant ID, research spec hash, research terminal file hash, and behavior-binding hash.

Its semantic identity is `canonical_sha256(payload)`. Its published pretty canonical bytes have a separate SHA-256. This object does not include `validation_spec_sha256`, avoiding a hash cycle; the validation hash binds the commitment hash through W1B's existing function.

The artifact path is content-addressed beneath the caller-provided artifact root:

```text
strategy_lab/top1/experiments/<experiment_id>/hidden_window_commitments/<commitment_sha256>.json
```

It is write-once/adopt-exact. Publishing before the validation-start transaction is safe: a failed transaction leaves only an inert content-addressed file with no SQLite authority or date occupancy; a later changed proposal uses a different path. Public reads enumerate only SQLite-bound refs and never discover orphan files.

## 7. Application commands and transitions

All business write commands accept explicit `actor`, canonical UTC `occurred_at`, and `idempotency_key`. Commands that may advance an experiment also receive `artifact_root` and optional test `environ`; they evaluate the exact maintainer gate plus SQLite opt-in before any future module may read market/provider data.

### Feature commands

- `set_account_opt_in(store, market, account, enabled, actor, occurred_at, idempotency_key, artifact_root, environ)` stores only user intent.
- Enabling requires the exact maintainer availability value `1`; when maintainer availability is off it returns `feature_disabled` without creating or changing the account feature row.
- Disabling commits `user_opt_in=false` first, then idempotently terminates every active account experiment with `experimental_feature_disabled/user`; a crash can leave projection pending but never re-enable writes.
- `reconcile_disabled_experiments(...)` handles maintainer-off with scope `maintainer` without changing user opt-in. It is idempotent and performs no market/provider read.
- Every normal write calls the same effective-gate check. When false, it first reconciles active termination, then raises `feature_disabled`.

### Draft and research

- `prepare_experiment()` validates a research-ready W1B spec, exact experiment/account identity, initial source/config/policy hashes, and computes the research hash. A same-byte retry is idempotent.
- While still draft, a changed valid spec replaces the single current spec and invalidates research plus validation authorization. Once research starts, the research-hash domain is immutable. After the completed research terminal is published, `lock_challenger()` may first add validation-only fields, and before validation starts may replace only those validation-only fields while the research hash remains exact; either change invalidates validation authorization.
- `authorize_research()` records a human confirmation only for the current exact research hash.
- `start_research()` requires effective feature, confirmed current hash, and the sealed historical dataset ref/hash already present in the spec. It atomically enters `research/building_dataset` and creates the research generation at revision 0 bound to that dataset. Research occupies no validation slot.
- `record_generation_revision()` is a narrow lifecycle CAS used later by W5/W6: exact next revision, ref/file hash, and frozen-row hash only. It stores no result rows and rejects any terminal-requested generation.
- `seal_generation()` requests one completed terminal for an open generation. It does not conclude the experiment or calculate a result.

### Leader lock and validation

- `lock_challenger()` requires a published completed research generation, a research receipt ref/hash, and a caller-supplied system leader equal to the requested non-baseline challenger. W3 checks membership and equality but does not calculate the leader.
- It validates a validation-ready W1B spec whose research hash is unchanged, builds/validates the 20-date commitment, computes the validation hash, stores one current proposal, and sets validation authorization to unconfirmed/invalidated. Before validation starts, changing only a valid future commitment/spec repeats this step and invalidates any previous validation confirmation.
- `authorize_validation()` confirms only the current exact validation hash.
- `start_validation()` first write-once publishes/adopts the content-addressed canonical commitment. In one SQLite transaction it rechecks effective opt-in state, exact authorization/spec/hash, exact-date uniqueness for all 20 dates, and the partial unique slot; then it inserts the 20 consumed date rows, enters `validation/collecting_decisions`, and creates hidden generation revision 0 bound to the commitment artifact.

### Validation writes and day-20 boundary

- `commit_validation_point()` records only point ID, trading date, source ref/hash, and the resulting hidden-manifest next revision/ref/file/frozen-row hashes. Multiple points may share one committed trading date. Same identity/bytes is idempotent; same point with different facts conflicts.
- `seal_validation_partition()` requires the next sequential commitment date, at least one committed official point for that date, and no later date already sealed. It increments only the completed-partition counter.
- Partitions 1–19 retain `collecting_decisions` and the slot. Partition 20 atomically closes point/partition intake, requests the hidden generation's completed terminal, sets `validation_progress=awaiting_outcomes`, and releases the slot. W3 cannot infer an empty outcome set from the absence of W6's table and offers no normal completion path. W6 later extends this atomic command to close its job-registration set, request an empty/non-empty outcome terminal, and choose `awaiting_outcomes|ready_to_conclude` from actual job rows.
- Any point, revision, or partition after terminal request/day 20 fails as `late_write`; legal appends do not change either authorization hash/status.

## 8. Terminal payload and projection

### Generation terminal

`build_generation_terminal_request()` creates exact canonical bytes for `sell_put_top1_generation_terminal.v1` with generation kind/ID, terminal mode/reason/scope/time, last revision/ref/file hash, frozen-row hash, nullable compact aborted-partial summary, and existing `research_artifact_provenance.v1`.

- Completed requires null reason/scope.
- Aborted reason is exactly `human_abandoned|behavior_binding_drift|experimental_feature_disabled`; only feature-disabled allows `disabled_scope=user|maintainer`.
- `terminal_sha256` is outside the terminal payload in SQLite/event rows, avoiding self-reference.

### Aborted experiment receipt

- `terminate_experiment()` builds W3's deterministic aborted `sell_put_top1_experiment_receipt.v1` with `outcome_status=insufficient_evidence`, no adoptable metrics, started-generation planned terminal refs/hashes, and not-started markers for absent generations.
- W3 does not expose `complete_experiment()`. W5/W6 add normal completion only after their exact research/validation facts and receipt schema validation exist. The store keeps only the minimal terminal-request/CAS primitive they will reuse.
- The aborted experiment terminal request is immutable. Same requested bytes are idempotent; a different mode or bytes conflict. W3's completed-vs-aborted competition is exercised at the generation terminal CAS.

### Recovery

The terminal transaction:

1. commits experiment terminal intent when applicable;
2. requests a terminal for every still-open generation that has no prior request, preserving already requested/published terminals;
3. appends one final receipt request last.

`recover_terminal_projection(store, artifact_root, publisher=None)` scans requested-but-unpublished targets in event order. The default publisher:

- rejects unsafe/symlink paths;
- writes requested UTF-8 bytes to a same-directory temporary file, file-fsyncs, atomically links/publishes or adopts exact existing bytes, and parent-fsyncs;
- treats same parsed JSON with different bytes as conflict.

After each successful file, a second SQLite transaction validates event/ref/content/file hashes, appends a published event, and CAS-updates its target. The experiment becomes `concluded` only when the receipt and every started generation are published. Crash/retry never re-renders bytes and never reads market/provider data.

## 9. Termination semantics

- `terminate_experiment()` is allowed even when feature gates are off and is the only normal write besides projection recovery allowed after disable.
- Terminal reason/scope and `terminated_at_partition` are immutable. Validation abort records the current completed partition count and permanently retains the full hidden commitment; research/draft abort uses null partition.
- Setting experiment terminal mode immediately blocks all new revisions, points, partitions, starts, and result requests, and releases an active validation slot even if artifact projection is pending.
- A completed generation request that wins before abort remains completed; abort requests only generations still open without a request. If abort wins first, a late completed seal affects zero rows and returns `experiment_terminated`/`terminal_conflict` without storing the late result.
- Feature disable terminates all active experiments with `experimental_feature_disabled`, the correct disabled scope, and caller actor. It never rewrites a previously terminal generation.

## 10. Public read projections

`read_public_status()` returns only:

- effective gate sources/status;
- experiment identity, phase/progress, authorization statuses and bound hashes;
- completed partition count, blocked reason, terminal mode/reason/scope, projection state;
- generation kind/state/revision/terminal mode/ref/hashes.

It never returns point identities, variant interim ranks, daily deltas, observation/outcome facts, metrics, or an interim winner.

`read_public_receipt()` returns `None` until phase is `concluded` and the receipt publication event is linked. It then parses the exact requested canonical bytes already verified by recovery. No filesystem or market read is performed by the store read.

## 11. Error contract

`Top1LifecycleError.reason_code` is limited to stable boundary failures:

```text
schema_unsupported
feature_disabled
experiment_invalid
experiment_conflict
invalid_transition
authorization_required
authorization_hash_mismatch
hidden_window_overlap
validation_slot_occupied
generation_conflict
late_write
terminal_conflict
projection_conflict
receipt_unavailable
```

SQLite integrity errors are translated at the application boundary; raw SQL/schema text is not exposed in public status.

## 12. Implementation slices

### S1 — Store, gate, authorization, slot, and append CAS

- Add schema/migration/store commands and lifecycle through point/partition commit.
- Add exact import guards and migration/gate/authorization/slot/late-write tests.
- No terminal file writer beyond commitment write-once publication.

Completion: fresh/v0/v1 migration, default-off/effective gate, maintainer-off enable no-write, separate authorization, research no-slot, exact-date commitment ownership, second active validation, natural-fact/idempotency conflicts, and day19/day20 slot tests pass.

### S2 — Terminal request, recovery, and public projection

- Add generation/aborted-receipt terminal builders, generation completed/aborted CAS, disable/abandon reconciliation, safe publisher, recovery, status, and receipt.
- Add all crash/concurrency/byte-conflict/non-leak tests.

Completion: all §14 tests pass and S1 remains green.

Each slice receives focused self-review. The complete W3 module then receives the user-required Kimi DeepReview, fixes/re-review, aggregate review, Draft PR, CI, PR-level Kimi review, and only separately authorized merge.

## 13. Validation commands

```text
python -m pytest -q tests/test_strategy_lab_top1_store.py tests/test_strategy_lab_top1.py tests/test_strategy_lab_top1_w1b.py tests/test_recommendation_point.py tests/test_strategy_lab_top1_architecture.py
python -m pytest -q tests/test_candidate_snapshot_manifest.py tests/test_opening_candidate_snapshot.py tests/test_daily_decision_brief_notification_flow.py tests/test_position_projection_migration.py
ruff check src/infrastructure/strategy_lab src/application/strategy_lab/top1/lifecycle.py src/application/strategy_lab/top1/terminal_projection.py src/application/shadow_replay/common.py tests/test_strategy_lab_top1_store.py tests/test_strategy_lab_top1_architecture.py
basedpyright --level error src/infrastructure/strategy_lab/experiment_store.py src/application/strategy_lab/top1/lifecycle.py src/application/strategy_lab/top1/terminal_projection.py
python scripts/generate_dependency_graph.py --check
python -m pytest -q
git diff --check
```

Full-suite environmental failures are classified with exact evidence; they are not represented as broad passes without closure.

## 14. Acceptance matrix

### Migration and storage

- Missing DB read is `not_initialized` without file creation; fresh/v0 migrate to v1; repeated v1 is identical; unknown/corrupt/partial v1 fails closed.
- SQLite file and sidecars remain private; no WAL is enabled.
- Exact schema constraints/indexes exist, including the required partial slot index; no future corpus/validation/outcome table exists.
- Stored specs/events are compact canonical JSON; terminal request payload is below 8 KiB.

### Gate and authorization

- Missing/non-`1` maintainer env and absent account row are false; maintainer false overrides opt-in true.
- Enabling while maintainer false changes no SQLite row; disabling remains idempotent even when maintainer false or the row is absent.
- Research hash change in draft invalidates research/downstream validation; validation-only change after leader lock invalidates only validation.
- Exact research/validation confirmation is required; stale/wrong hashes fail; legal hidden append does not invalidate either.

### Slot and commitment

- Multiple research experiments can start concurrently without occupying a slot.
- Validation start rejects same account active collector, any exact historical trading-date overlap, wrong 20-date order/count, stale authorization, or same-path byte conflict.
- A pair of commitments whose start/end envelopes intersect but whose exact dates do not may coexist; one shared exact date always conflicts.
- A commitment file published before a failed start transaction has no SQLite authority and cannot block a changed content-addressed proposal.
- Day 19 retains slot and rejects second collector; day 20 request/close transaction rejects late point/partition and permits a non-overlapping new collector while the old experiment remains `awaiting_outcomes`.
- Aborting validation keeps all 20 commitment date rows and releases only the active collection slot.

### Idempotency, terminal competition, and recovery

- Same prepare/auth/start/point/partition/terminal command is idempotent; same identity with different bytes conflicts; double writers create one event/point/request. Different caller keys cannot duplicate one natural point/partition/terminal fact, and reuse of one caller key with different bytes conflicts.
- Completed request first then abort preserves completed generation bytes; abort first rejects late completed seal; one generation never has two terminal requests.
- Injected crash after terminal DB commit, after each file publish, and before/after publication CAS recovers the exact requested bytes/ref/content/file hashes without duplicate requested/published events.
- Existing exact file adopts; same JSON content with a byte difference conflicts and leaves DB pending.
- Feature disable before/during research, after research completion but before validation, during validation, and while projection pending blocks new writes immediately and converges to one aborted receipt with correct scope/reason.

### Read isolation

- Validation status throughout days 1–20 contains no point IDs, ranks, arm metrics, daily deltas, outcome facts, or leader conclusion.
- In W3, receipt is unavailable before conclusion and exact after aborted projection CAS; normal completed receipt remains absent until W5/W6 add and validate it.
- Terminal/recovery paths make zero market/provider/recommendation-point reads.

## 15. Risks and deferred ownership

- W4 owns corpus rows and long-term ranking projections.
- W5 owns research result computation, leader policy, research receipt payload, and any research-only normal conclusion command.
- W6 owns validation rows, outcome jobs, day-20 job-aware progress choice, metrics, and the normal final completion command/receipt validation.
- W7 owns CLI/service/profile/timer surfaces and installed effective-gate evidence.
- W8 owns LLM/Prompt/advisory provenance.
- W3 deliberately uses one SQLite writer and synchronous artifact recovery calls; add a worker/queue only if measured runtime contention or recovery latency requires it.

## 16. No-overdesign check

The design adds exactly five current-state tables, one append-only event table as the outbox, two application modules, and no new dependency. It stores only facts required for restart, authorization, slot, and terminal recovery. Later data tables and successful-result policy remain absent.

## 17. Gate sequence

1. PlanReview this draft against the goal and current code.
2. Fix every accepted plan finding and re-review until pass.
3. Commit the accepted plan.
4. Implement S1, verify, then S2 and verify.
5. Run Kimi Current Changes DeepReview; fix/re-review to zero unresolved accepted findings.
6. Commit accepted implementation and run Kimi aggregate committed-range review.
7. Open Draft PR, wait required CI, run Kimi PR review, record closeout.
8. Ready/merge only under explicit user authorization; never release or deploy implicitly.
